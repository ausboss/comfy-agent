"""The GPU handoff's building blocks: job journal, GPU lease, inference adapters. Standard library only.

comfy_agent.py uses these to run a managed operation. The contract is docs/lifecycle.md.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

OP_FINAL = ("done", "failed", "cancelled", "blocked")
JOB_FINAL = ("done", "failed", "lost", "skipped", "cancelled")


def now():
    return time.time()


def new_op_id():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


# ---------------------------------------------------------------- journal

SCHEMA = """
CREATE TABLE IF NOT EXISTS ops (
  id TEXT PRIMARY KEY, created REAL, updated REAL, state TEXT, mode TEXT, recipe TEXT,
  error TEXT, cancel INTEGER DEFAULT 0, pid INTEGER, heartbeat REAL, data TEXT);
CREATE TABLE IF NOT EXISTS jobs (
  rowid INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT, n INTEGER, state TEXT, prompt_id TEXT,
  delivered INTEGER DEFAULT 0, updated REAL, data TEXT);
CREATE INDEX IF NOT EXISTS jobs_op ON jobs(op);
CREATE INDEX IF NOT EXISTS jobs_prompt ON jobs(prompt_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Journal:
    """Operations and their jobs in SQLite. Every write is its own short transaction, so a crash loses
    nothing that was already written, and separate processes and threads can share the file."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.conn().executescript(SCHEMA)

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            with contextlib.suppress(sqlite3.OperationalError):
                c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    @contextlib.contextmanager
    def tx(self):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    def close(self):
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # ops
    def create_op(self, op_id, mode, recipe, data, jobs):
        t = now()
        with self.tx() as c:
            c.execute("INSERT INTO ops (id, created, updated, state, mode, recipe, data) VALUES (?,?,?,?,?,?,?)",
                      (op_id, t, t, "queued" if mode == "managed" else "submitting", mode, recipe, json.dumps(data)))
            for job in jobs:
                c.execute("INSERT INTO jobs (op, n, state, prompt_id, updated, data) VALUES (?,?,?,?,?,?)",
                          (op_id, job["n"], job.get("state", "planned"), job.get("id"), t, json.dumps(job)))

    def op(self, op_id):
        r = self.conn().execute("SELECT * FROM ops WHERE id=?", (op_id,)).fetchone()
        return self._op(r) if r else None

    @staticmethod
    def _op(r):
        d = dict(r)
        d["data"] = json.loads(d["data"] or "{}")
        return d

    def ops(self, limit=20, unfinished=False):
        q = "SELECT * FROM ops"
        if unfinished:
            q += " WHERE state NOT IN (%s)" % ",".join("?" * len(OP_FINAL))
        q += " ORDER BY created DESC LIMIT ?"
        args = (*OP_FINAL, limit) if unfinished else (limit,)
        return [self._op(r) for r in self.conn().execute(q, args).fetchall()]

    def blocked_ops(self):
        return [self._op(r) for r in self.conn().execute(
            "SELECT * FROM ops WHERE state='blocked' ORDER BY created").fetchall()]

    def set_op(self, op_id, **fields):
        data = fields.pop("data", None)
        with self.tx() as c:
            if data is not None:
                row = c.execute("SELECT data FROM ops WHERE id=?", (op_id,)).fetchone()
                merged = json.loads(row["data"] or "{}") if row else {}
                merged.update(data)
                fields["data"] = json.dumps(merged)
            fields["updated"] = now()
            cols = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE ops SET {cols} WHERE id=?", (*fields.values(), op_id))

    def append(self, op_id, key, value):
        """Add value to the list data[key] of an operation, atomically."""
        with self.tx() as c:
            row = c.execute("SELECT data FROM ops WHERE id=?", (op_id,)).fetchone()
            data = json.loads(row["data"] or "{}")
            data.setdefault(key, []).append(value)
            c.execute("UPDATE ops SET data=?, updated=? WHERE id=?", (json.dumps(data), now(), op_id))

    def claim(self, op_id, seen_heartbeat):
        """Take over a dead runner's operation. Only one caller wins."""
        with self.tx() as c:
            cur = c.execute("UPDATE ops SET heartbeat=?, pid=NULL WHERE id=? AND heartbeat IS ?",
                            (now(), op_id, seen_heartbeat))
            return cur.rowcount == 1

    def heartbeat(self, op_id, pid=None):
        with self.tx() as c:
            if pid is None:
                c.execute("UPDATE ops SET heartbeat=? WHERE id=?", (now(), op_id))
            else:
                c.execute("UPDATE ops SET heartbeat=?, pid=? WHERE id=?", (now(), pid, op_id))

    # jobs
    def jobs(self, op_id=None, states=None, undelivered=False):
        q, args = "SELECT * FROM jobs WHERE 1=1", []
        if op_id:
            q += " AND op=?"
            args.append(op_id)
        if states:
            q += " AND state IN (%s)" % ",".join("?" * len(states))
            args += list(states)
        if undelivered:
            q += " AND delivered=0"
        q += " ORDER BY rowid"
        return [self._job(r) for r in self.conn().execute(q, args).fetchall()]

    @staticmethod
    def _job(r):
        job = json.loads(r["data"])
        job.update(rowid=r["rowid"], op=r["op"], state=r["state"], delivered=r["delivered"])
        if r["prompt_id"]:
            job["id"] = r["prompt_id"]
        return job

    def set_job(self, job, state=None, **extra):
        """Write a job dict back. Keys in extra are merged into it first."""
        job.update(extra)
        if state:
            job["state"] = state
        body = {k: v for k, v in job.items() if k not in ("rowid", "op", "delivered")}
        with self.tx() as c:
            c.execute("UPDATE jobs SET state=?, prompt_id=?, updated=?, data=? WHERE rowid=?",
                      (job.get("state"), job.get("id"), now(), json.dumps(body), job["rowid"]))

    def job_by_prompt(self, prompt_id):
        r = self.conn().execute("SELECT * FROM jobs WHERE prompt_id=? ORDER BY rowid DESC LIMIT 1",
                                (prompt_id,)).fetchone()
        return self._job(r) if r else None

    def mark_delivered(self, rowids):
        if not rowids:
            return
        with self.tx() as c:
            c.executemany("UPDATE jobs SET delivered=1 WHERE rowid=?", [(r,) for r in rowids])

    # meta
    def get_meta(self, key, default=None):
        r = self.conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_meta(self, key, value):
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))


# ---------------------------------------------------------------- lease

class Lease:
    """One GPU lease per output folder: an OS file lock, dropped by the OS if the holder dies.
    flock locks belong to the open file, so two threads of one process also exclude each other."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.lock_path = self.folder / ".gpu.lock"
        self.info_path = self.folder / ".gpu.lease.json"
        self.fd = None

    def _try(self, fd):
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(self, fd):
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            with contextlib.suppress(OSError):
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)

    def acquire(self, owner, timeout=None, poll=0.2, on_wait=None, should_stop=None):
        """Wait for the lease. Returns True when held; False on timeout or when should_stop() says so."""
        self.folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        start, told = now(), False
        while not self._try(fd):
            if not told and on_wait:
                on_wait(self.holder())
                told = True
            if (timeout is not None and now() - start > timeout) or (should_stop and should_stop()):
                os.close(fd)
                return False
            time.sleep(poll)
        self.fd = fd
        info = {"op": owner, "pid": os.getpid(), "host": socket.gethostname(), "since": now()}
        tmp = self.info_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(info), encoding="utf-8")
        tmp.replace(self.info_path)
        return True

    def release(self):
        if self.fd is None:
            return
        with contextlib.suppress(OSError):
            self.info_path.unlink()
        self._unlock(self.fd)
        os.close(self.fd)
        self.fd = None

    def held_by_anyone(self):
        if not self.lock_path.exists():
            return False
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if self._try(fd):
                self._unlock(fd)
                return False
            return True
        finally:
            os.close(fd)

    def holder(self):
        try:
            return json.loads(self.info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        return True  # no cheap check; the heartbeat decides
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------- inference adapters

class AdapterError(Exception):
    pass


def http_json(url, method="GET", body=None, headers=None, timeout=30):
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        e.close()
        raise AdapterError(f"{method} {url}: HTTP {e.code} {raw.decode('utf-8', 'replace')[:200]}") from None
    except (urllib.error.URLError, OSError) as e:
        raise AdapterError(f"{method} {url}: {getattr(e, 'reason', e)}") from None
    try:
        return json.loads(raw) if raw.strip() else None
    except ValueError:
        return raw.decode("utf-8", "replace")


class Adapter:
    """Takes the agent's model off the GPU and puts it back. Subclasses answer three questions:
    what is loaded, unload these, load these. loaded() returns None when it can't tell."""
    kind = "none"
    managed = False

    def __init__(self, cfg):
        self.cfg = cfg

    def loaded(self):
        return []

    def unload(self, models):
        pass

    def load(self, models):
        pass

    def is_unloaded(self, models):
        return True

    def is_ready(self, models):
        return True

    def describe(self):
        return "no local model"


class CommandAdapter(Adapter):
    """Shell commands. The legacy unload_llm. Without a load command the server loads by itself."""
    kind = "command"
    managed = True

    def _run(self, key, timeout):
        cmd = self.cfg.get(key)
        if not cmd:
            return None
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AdapterError(f"{key} command took over {timeout} s: {cmd}") from None
        if key != "ready" and r.returncode:
            raise AdapterError(f"{key} command failed ({cmd}): {(r.stderr or r.stdout).strip()[:300]}")
        return r.returncode

    def loaded(self):
        return [self.cfg.get("model") or "the language model"]

    def unload(self, models):
        self._run("unload", int(self.cfg.get("unload_timeout", 120)))

    def load(self, models):
        self._run("load", int(self.cfg.get("load_timeout", 300)))

    def is_unloaded(self, models):
        return None  # a command can't tell us; the freed memory is the evidence

    def is_ready(self, models):
        code = self._run("ready", 30)
        return True if code is None else code == 0

    def describe(self):
        return f"command adapter (unload: {self.cfg.get('unload')})"


class HttpAdapter(Adapter):
    managed = True

    def __init__(self, cfg):
        super().__init__(cfg)
        self.url = str(cfg.get("url") or "http://127.0.0.1:8080").rstrip("/")
        key = cfg.get("api_key") or (os.environ.get(cfg["api_key_env"]) if cfg.get("api_key_env") else None)
        self.headers = {"Authorization": "Bearer " + key} if key else {}

    def call(self, path, method="GET", body=None, timeout=30):
        return http_json(self.url + path, method, body, self.headers, timeout)

    def only(self, names):
        want = self.cfg.get("model")
        return [m for m in names if m == want] if want else list(names)


class RouterAdapter(HttpAdapter):
    """llama.cpp llama-server in router mode: GET /models, POST /models/load and /models/unload.
    Start it with --no-models-autoload so nothing reloads behind the lease."""
    kind = "llama-router"

    def models(self):
        res = self.call("/models") or {}
        return {m.get("id"): (m.get("status") or {}) for m in res.get("data", []) if m.get("id")}

    def loaded(self):
        return self.only([k for k, s in self.models().items() if s.get("value") in ("loaded", "loading")])

    def unload(self, models):
        for m in models:
            self.call("/models/unload", "POST", {"model": m}, timeout=int(self.cfg.get("unload_timeout", 120)))

    def load(self, models):
        for m in models:
            self.call("/models/load", "POST", {"model": m}, timeout=int(self.cfg.get("load_timeout", 300)))

    def is_unloaded(self, models):
        st = self.models()
        return all(st.get(m, {}).get("value") in (None, "unloaded", "sleeping") for m in models)

    def is_ready(self, models):
        st = self.models()
        for m in models:
            s = st.get(m, {})
            if s.get("failed"):
                raise AdapterError(f"{m} failed to load (exit code {s.get('exit_code')})")
            if s.get("value") != "loaded":
                return False
        return True

    def describe(self):
        try:
            st = self.models()
        except AdapterError as e:
            return f"llama.cpp router at {self.url}: not answering ({e})"
        shown = ", ".join(f"{k} {v.get('value')}" for k, v in st.items()) or "no models"
        return f"llama.cpp router at {self.url}: {shown}"


class SwapAdapter(HttpAdapter):
    """llama-swap. POST /api/models/pause unloads and rejects requests until /api/models/resume; older
    builds only have an unload, so a stray request can reload the model there."""
    kind = "llama-swap"

    def running(self):
        res = self.call("/running") or {}
        items = res.get("running", []) if isinstance(res, dict) else []
        return {i.get("model"): i.get("state", "ready") for i in items if isinstance(i, dict)}

    def loaded(self):
        return self.only([m for m, s in self.running().items() if s not in ("stopped", "shutdown")])

    def unload(self, models):
        try:
            self.call("/api/models/pause", "POST", timeout=int(self.cfg.get("unload_timeout", 120)))
            self.paused = True
            return
        except AdapterError as e:
            if "HTTP 404" not in str(e) and "HTTP 405" not in str(e):
                raise
        self.paused = False
        if self.cfg.get("model"):
            for m in models:
                self.call("/api/models/unload/" + urllib.request.quote(m, safe=""), "POST")
            return
        try:
            self.call("/api/models/unload", "POST")
        except AdapterError as e:
            if "HTTP 404" not in str(e) and "HTTP 405" not in str(e):
                raise
            self.call("/unload")

    def load(self, models):
        with contextlib.suppress(AdapterError):
            self.call("/api/models/resume", "POST")
        for m in models:  # any request through /upstream/MODEL starts it
            self.call("/upstream/" + urllib.request.quote(m, safe="") + "/health",
                      timeout=int(self.cfg.get("load_timeout", 300)))

    def is_unloaded(self, models):
        live = self.running()
        return not any(m in live and live[m] not in ("stopped", "shutdown") for m in models)

    def is_ready(self, models):
        live = self.running()
        return all(live.get(m) == "ready" for m in models)

    def describe(self):
        try:
            live = self.running()
        except AdapterError as e:
            return f"llama-swap at {self.url}: not answering ({e})"
        return f"llama-swap at {self.url}: " + (", ".join(f"{k} {v}" for k, v in live.items()) or "nothing running")


ADAPTERS = {"none": Adapter, "command": CommandAdapter, "llama-router": RouterAdapter, "llama-swap": SwapAdapter}


def make_adapter(cfg):
    cfg = dict(cfg or {})
    kind = cfg.get("type") or "none"
    if kind not in ADAPTERS:
        raise AdapterError(f"inference.type must be one of {', '.join(ADAPTERS)}, not '{kind}'")
    return ADAPTERS[kind](cfg)


# ---------------------------------------------------------------- free memory, as ComfyUI sees it

def free_mb(stats, device=0):
    """Free device memory in MiB that another process could use, from ComfyUI's /system_stats.

    vram_free includes memory PyTorch has reserved but isn't using; only ComfyUI can use that part, so it
    is taken off. Returns None when ComfyUI reports no GPU (CPU-only), so no check is possible."""
    devices = [d for d in (stats or {}).get("devices", []) if d.get("type") != "cpu"]
    if not devices:
        return None
    d = devices[min(int(device), len(devices) - 1)]
    free = d.get("vram_free")
    if free is None:
        return None
    return int((free - (d.get("torch_vram_free") or 0)) / (1 << 20))
