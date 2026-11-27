#!/usr/bin/env python3
"""comfy-agent: let any AI agent make pictures and videos with ComfyUI.

Python 3.9 or newer, standard library only (this file and comfy_lifecycle.py). It talks to ComfyUI over its HTTP API.

  comfy_agent.py list                         the recipes, one line each
  comfy_agent.py show RECIPE [--notes]        a recipe's settings, defaults and tips
  comfy_agent.py run RECIPE [name=value ...]  queue it, wait, save the results
        [--count N] [--send [TARGET]] [--no-wait] [--free] [--set NODE.input=VALUE]
  comfy_agent.py wait [--send [TARGET]]       wait for queued jobs and save their results
  comfy_agent.py send [FILE ... | last] [--to TARGET]
  comfy_agent.py doctor                       is ComfyUI up, which recipes are ready
  comfy_agent.py install RECIPE [--yes]       install a recipe's node packs and models
  comfy_agent.py restart                      restart ComfyUI (after new node packs)
  comfy_agent.py add WORKFLOW.json --name NAME  turn a ComfyUI workflow into a recipe
  comfy_agent.py gallery [--off]              the results as a private web page on your tailnet
  comfy_agent.py config [key=value ...]       url, out, and where "phone" is
  comfy_agent.py mcp [--http PORT --token T]  the same thing as an MCP server with 3 tools
  comfy_agent.py ops [OP] [--json] | cancel [OP] | reconcile   the GPU handoff (docs/lifecycle.md)
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import datetime as dt
import difflib
import hashlib
import html
import http.client
import io
import json
import mimetypes
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from comfy_lifecycle import (JOB_FINAL, OP_FINAL, AdapterError, Journal, Lease, default_lease_dir, free_mb,
                             make_adapter, new_op_id, pid_alive)

KIT = Path(__file__).resolve().parent
CONFIG = KIT / "comfy-agent.local.json"
MODEL_EXT = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".sft", ".onnx")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv")
MEDIA_KINDS = ("image", "video", "audio", "file")
CLIENT_ID = str(uuid.uuid4())


class Fail(Exception):
    """A problem to explain to the person. Printed without a traceback."""


class ComfyError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {str(detail)[:300]}")
        self.status = status
        self.detail = detail


def load_settings():
    s = {"url": "http://127.0.0.1:8188", "out": str(KIT / "outputs"), "send": {}}
    if CONFIG.exists():
        s.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    s["url"] = os.environ.get("COMFY_URL", s["url"]).rstrip("/")
    if os.environ.get("COMFY_AGENT_OUT"):
        s["out"] = os.environ["COMFY_AGENT_OUT"]
    if os.environ.get("COMFY_AGENT_UNLOAD_LLM"):
        s["unload_llm"] = os.environ["COMFY_AGENT_UNLOAD_LLM"]
    if os.environ.get("COMFY_AGENT_INFERENCE"):
        s["inference"] = json.loads(os.environ["COMFY_AGENT_INFERENCE"])
    return s


S = load_settings()


def out_dir():
    return Path(S["out"]).expanduser()


# ---------------------------------------------------------------- HTTP

def open_url(url, method="GET", body=None, headers=None, timeout=60):
    headers = dict(headers or {})
    data = body
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def comfy(path, body=None, method=None, headers=None, timeout=60, raw=False):
    """Call ComfyUI. Returns parsed JSON, or bytes when raw."""
    try:
        with open_url(S["url"] + path, method or ("GET" if body is None else "POST"), body, headers, timeout) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        payload = e.read()
        e.close()
        try:
            detail = json.loads(payload)
        except ValueError:
            detail = payload.decode("utf-8", "replace")[:300]
        raise ComfyError(e.code, detail) from None
    except (urllib.error.URLError, OSError) as e:
        raise Fail(f"ComfyUI is not answering at {S['url']} ({getattr(e, 'reason', e)}).\n"
                   "Start ComfyUI first. If it runs on another address: python3 comfy_agent.py config url=http://HOST:PORT") from None
    if raw:
        return payload
    return json.loads(payload) if payload.strip() else None


def download(url, dest, headers=None, quiet=True):
    """Stream url to dest through a .part file, resuming a partial download."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    headers = dict(headers or {})
    have = part.stat().st_size if part.exists() else 0
    if have:
        headers["Range"] = f"bytes={have}-"
    try:
        with open_url(url, headers=headers, timeout=120) as r:
            mode = "ab" if have and r.status == 206 else "wb"
            total = r.headers.get("Content-Length")
            total = int(total) + (have if mode == "ab" else 0) if total else None
            done = have if mode == "ab" else 0
            mark = 0
            with open(part, mode) as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if not quiet and total and done * 4 // total > mark:
                        mark = done * 4 // total
                        print(f"  {dest.name}: {mark * 25}% of {gb(total)}", flush=True)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise Fail(f"{url} needs a login (HTTP {e.code}). Set HF_TOKEN (Hugging Face) or CIVITAI_API_KEY (Civitai) "
                       "and try again. Some models also need their license accepted on the website first.") from None
        raise Fail(f"Download failed, HTTP {e.code}: {url}") from None
    except (urllib.error.URLError, OSError) as e:
        raise Fail(f"Download stopped ({e}). Run the same command again to resume.") from None
    part.replace(dest)
    return dest


def env_first(*names):
    return next((os.environ[n] for n in names if os.environ.get(n)), None)


def auth_headers(url):
    host = urllib.parse.urlparse(url).netloc
    key = None
    if host.endswith("huggingface.co"):
        key = env_first("HF_TOKEN", "HUGGINGFACE_TOKEN", "HF_API_TOKEN")
    elif host.endswith("civitai.com"):
        key = env_first("CIVITAI_API_KEY", "CIVITAI_API_TOKEN", "CIVITAI_TOKEN")
    return {"Authorization": "Bearer " + key} if key else {}


def gb(n):
    return f"{n / 1e9:.1f} GB" if n is not None else "unknown size"


# ---------------------------------------------------------------- recipes

def recipe_dirs():
    dirs = [KIT / "recipes"]
    dirs += [Path(p).expanduser() for p in os.environ.get("COMFY_AGENT_RECIPES", "").split(os.pathsep) if p]
    dirs += [Path(p).expanduser() for p in S.get("recipes", [])]
    return [d for d in dirs if d.is_dir()]


def all_recipes():
    found = {}
    for d in recipe_dirs():
        for card in sorted(d.glob("*/card.json")):
            found.setdefault(card.parent.name, card.parent)
    return found


class Recipe:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.name = self.folder.name
        self.card = json.loads((self.folder / "card.json").read_text(encoding="utf-8"))
        self.inputs = self.card.get("inputs", {})

    def prompt(self):
        return json.loads((self.folder / "prompt.json").read_text(encoding="utf-8"))


def get_recipe(name):
    p = Path(name).expanduser()
    if (p / "card.json").exists():
        return Recipe(p)
    found = all_recipes()
    if name in found:
        return Recipe(found[name])
    near = difflib.get_close_matches(name, list(found), n=3, cutoff=0.4)
    hint = f" Did you mean {' or '.join(near)}?" if near else ""
    raise Fail(f"No recipe called '{name}'.{hint} See them all: python3 comfy_agent.py list")


def kind_of(key, spec):
    if spec.get("type"):
        return spec["type"]
    if key == "seed":
        return "seed"
    d = spec.get("default")
    if isinstance(d, bool):
        return "bool"
    if isinstance(d, (int, float)):
        return "number"
    return "text"


def targets(spec):
    t = spec.get("set", [])
    return [t] if isinstance(t, str) else list(t)


def put(prompt, addr, value):
    nid, _, key = addr.partition(".")
    node = prompt.get(nid)
    if node is None:
        raise Fail(f"The recipe points at node {nid}, which is not in its prompt.json.")
    if key not in node["inputs"]:
        raise Fail(f"Node {nid} ({node['class_type']}) has no input '{key}'. It has: {', '.join(node['inputs'])}")
    if isinstance(node["inputs"][key], list):
        raise Fail(f"{addr} is wired to another node, so it can't be set to a value.")
    node["inputs"][key] = value


def coerce(value, kind):
    if kind == "number":
        for cast in (int, float):
            try:
                return cast(value)
            except (TypeError, ValueError):
                pass
        raise Fail(f"'{value}' is not a number.")
    if kind == "bool":
        return str(value).lower() in ("1", "true", "yes", "on")
    return value


def json_or_text(value):
    try:
        return json.loads(value)
    except ValueError:
        return value


def parse_pairs(items):
    pairs = []
    for item in items:
        if "=" not in item:
            raise Fail(f"'{item}' should look like name=value.")
        k, v = item.split("=", 1)
        pairs.append((k.strip(), v))
    return pairs


def plan_jobs(recipe, pairs, count):
    """One dict of settings per job. A setting given several times queues one job per value
    (several such settings are paired in order). count repeats each."""
    given = {}
    for k, v in pairs:
        if k not in recipe.inputs:
            raise Fail(f"{recipe.name} has no setting '{k}'. Its settings: {', '.join(recipe.inputs)}")
        given.setdefault(k, []).append(v)
    lengths = {len(v) for v in given.values() if len(v) > 1}
    if len(lengths) > 1:
        raise Fail("Settings given more than once are paired in order, so give each one the same number of times.")
    rounds = lengths.pop() if lengths else 1
    for key, spec in recipe.inputs.items():
        if spec.get("required") and key not in given:
            raise Fail(f"{recipe.name} needs {key}=... ({spec.get('help') or kind_of(key, spec)}).")
    return [{k: (v[i] if len(v) > 1 else v[0]) for k, v in given.items()} for i in range(rounds) for _ in range(count)]


# ---------------------------------------------------------------- what the server has

_node_info = {}
_model_lists = {}


def node_info(class_type):
    if class_type not in _node_info:
        try:
            info = comfy("/object_info/" + urllib.parse.quote(class_type, safe="")) or {}
        except ComfyError:
            info = {}
        _node_info[class_type] = info.get(class_type)
    return _node_info[class_type]


def input_spec(class_type, name):
    info = node_info(class_type) or {}
    for section in ("required", "optional"):
        spec = (info.get("input", {}).get(section) or {}).get(name)
        if spec is not None:
            return spec
    return None


def combo_options(class_type, name):
    spec = input_spec(class_type, name)
    if not spec:
        return None
    if isinstance(spec[0], list):
        return spec[0]
    if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1].get("options")
    return None


def models_in(folder):
    if folder not in _model_lists:
        try:
            _model_lists[folder] = comfy("/models/" + urllib.parse.quote(folder)) or []
        except ComfyError:
            _model_lists[folder] = []
    return _model_lists[folder]


def base_name(path):
    return str(path).replace("\\", "/").rsplit("/", 1)[-1]


def is_model_name(value):
    return isinstance(value, str) and value.lower().endswith(MODEL_EXT)


def preflight(prompt):
    """Point model names at wherever this ComfyUI keeps the same file, in place.
    Returns (missing node types, missing model files)."""
    missing_nodes, missing_models = [], []
    for node in prompt.values():
        if node_info(node["class_type"]) is None:
            missing_nodes.append(node["class_type"])
            continue
        for key, val in node["inputs"].items():
            if not is_model_name(val):
                continue
            opts = combo_options(node["class_type"], key)
            if not opts or val in opts:
                continue
            same = [o for o in opts if isinstance(o, str) and base_name(o) == base_name(val)]
            if same:
                node["inputs"][key] = same[0]
            else:
                missing_models.append(base_name(val))
    return sorted(set(missing_nodes)), sorted(set(missing_models))


def not_ready_message(recipe, nodes, models):
    parts = []
    if nodes:
        parts.append(f"node types {', '.join(nodes)}")
    if models:
        parts.append(f"model files {', '.join(models)}")
    return (f"{recipe.name} can't run yet: this ComfyUI is missing {' and '.join(parts)}.\n"
            f"See what would be installed: python3 comfy_agent.py install {recipe.name}")


# ---------------------------------------------------------------- run

def upload(path, cache):
    p = Path(path).expanduser()
    if str(p) in cache:
        return cache[str(p)]
    if not p.is_file():
        raise Fail(f"Can't find the file {path}")
    data = p.read_bytes()
    name = hashlib.sha1(data).hexdigest()[:8] + "-" + re.sub(r"[^\w.-]+", "_", p.name)
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n").encode() + data + (
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n--{boundary}--\r\n').encode()
    res = comfy("/upload/image", body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=300)
    cache[str(p)] = f"{res['subfolder']}/{res['name']}" if res.get("subfolder") else res["name"]
    return cache[str(p)]


def explain(detail):
    if not isinstance(detail, dict):
        return str(detail)[:400]
    lines = []
    err = detail.get("error")
    if isinstance(err, dict) and err.get("message"):
        lines.append(err["message"])
    for nid, ne in (detail.get("node_errors") or {}).items():
        for e in ne.get("errors", []):
            lines.append(f"node {nid} ({ne.get('class_type')}): {e.get('message', '')}: {str(e.get('details', ''))[:200]}")
    return "\n".join(lines)[:1500] or json.dumps(detail)[:400]


def run_error(status):
    for kind, data in status.get("messages", []):
        if kind == "execution_error":
            return f"{data.get('node_type')} (node {data.get('node_id')}): {str(data.get('exception_message', '')).strip()[:400]}"
        if kind == "execution_interrupted":
            return "the job was cancelled in ComfyUI"
    return "ComfyUI reported an error with no message"


STALE = float(os.environ.get("COMFY_AGENT_STALE", 20))   # seconds without a heartbeat = the runner died
POLL = float(os.environ.get("COMFY_AGENT_POLL", 1))       # how often to look at ComfyUI while waiting
_journals = {}
_children = []


def inference_cfg():
    """The model-server adapter's settings. The legacy unload_llm command means the command adapter."""
    cfg = dict(S.get("inference") or {})
    if not cfg.get("type") and S.get("unload_llm"):
        cfg = {"type": "command", "unload": S["unload_llm"], **cfg}
    cfg.setdefault("type", "none")
    return cfg


def managed():
    return inference_cfg()["type"] != "none"


def lease_dir():
    """The GPU lease's folder: inference.lease_dir, else one per computer shared by every output folder."""
    d = inference_cfg().get("lease_dir")
    return Path(d).expanduser() if d else default_lease_dir()


def journal():
    path = out_dir() / ".comfy-agent.db"
    j = _journals.get(str(path))
    if j is None:
        j = _journals[str(path)] = Journal(path)
        import_legacy_state(j)
    return j


def state_file():
    return out_dir() / ".comfy-agent-jobs.json"


def import_legacy_state(j):
    """Move the pre-journal .comfy-agent-jobs.json into the journal, once."""
    f = state_file()
    if not f.exists():
        return
    try:
        old = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    pending = [dict(x, state="queued") for x in old.get("pending", []) if x.get("id")]
    if pending:
        j.create_op("legacy-" + new_op_id(), "inline", pending[0].get("recipe"), {}, pending)
    if old.get("last"):
        j.set_meta("last", old["last"])
    with contextlib.suppress(OSError):
        f.replace(f.with_suffix(".json.imported"))


def load_state():
    """What older code called the state: jobs still being collected in this process's way, and the last files."""
    j = journal()
    inline = {o["id"] for o in j.ops(limit=200) if o["mode"] == "inline"}
    pending = [x for x in j.jobs(states=("planned", "submitting", "uncertain", "queued")) if x["op"] in inline]
    return {"pending": pending, "last": j.get_meta("last", [])}


def build_jobs(recipe, pairs, count=1, raw_sets=()):
    """Validate everything and build every prompt, uploads included, before anything touches the GPU."""
    jobs_settings = plan_jobs(recipe, pairs, count)
    base = recipe.prompt()
    nodes, models = preflight(base)
    if nodes or models:
        raise Fail(not_ready_message(recipe, nodes, models))
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    uploads, jobs = {}, []
    for i, given in enumerate(jobs_settings):
        prompt = copy.deepcopy(base)
        job = {"recipe": recipe.name, "n": i + 1, "of": len(jobs_settings), "stamp": stamp, "settings": {}}
        for key, spec in recipe.inputs.items():
            kind = kind_of(key, spec)
            value = given.get(key, spec.get("default"))
            if kind == "seed":
                value = random.randint(1, 2 ** 48) if value in (None, "", "random") else int(value) + i
                job["seed"] = value
            elif value is None or value == "":
                continue
            elif kind in MEDIA_KINDS:
                if key not in given:
                    continue
                value = upload(value, uploads)
            else:
                value = coerce(value, kind)
            for addr in targets(spec):
                put(prompt, addr, value)
            if key in given or kind == "seed":
                job["settings"][key] = value if not isinstance(value, str) or len(value) <= 120 else value[:117] + "..."
        for addr, value in raw_sets:
            put(prompt, addr, json_or_text(value))
        job["prompt"] = prompt
        jobs.append(job)
    return jobs


# ---- submitting: the prompt id is ours, so a lost answer can be looked up instead of sent again

def submit_job(j, job):
    """POST one job. Returns queued, refused or uncertain. Raises Fail if ComfyUI isn't listening."""
    pid = str(uuid.uuid4())
    j.set_job(job, "submitting", id=pid)
    body = {"prompt": job["prompt"], "client_id": CLIENT_ID, "prompt_id": pid, "extra_data": {"comfy_agent_job": pid}}
    try:
        with open_url(S["url"] + "/prompt", "POST", body, timeout=60) as r:
            res = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        payload = e.read()
        e.close()
        try:
            detail = json.loads(payload)
        except ValueError:
            detail = payload.decode("utf-8", "replace")[:300]
        if e.code < 500:
            job.pop("prompt", None)
            j.set_job(job, "failed", error="ComfyUI refused the job:\n" + explain(detail))
            return "refused"
        j.set_job(job, "uncertain", error=f"HTTP {e.code} from ComfyUI while submitting")
        return "uncertain"
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            job.pop("id", None)
            j.set_job(job, "planned")
            raise Fail(f"ComfyUI is not answering at {S['url']} ({e.reason}).") from None
        j.set_job(job, "uncertain", error=f"connection problem while submitting ({e.reason})")
        return "uncertain"
    except (OSError, http.client.HTTPException, ValueError) as e:
        j.set_job(job, "uncertain", error=f"the answer to the submit was lost ({e.__class__.__name__})")
        return "uncertain"
    job.pop("prompt", None)
    job.pop("error", None)
    j.set_job(job, "queued", id=res.get("prompt_id") or pid)
    return "queued"


def find_in_comfy(pid):
    """Is our prompt id (or our marker, for servers that pick their own ids) in ComfyUI's queue or history?
    Returns ComfyUI's prompt id, or None. Raises Fail if ComfyUI isn't answering."""
    if (comfy(f"/history/{pid}") or {}).get(pid):
        return pid
    q = comfy("/queue") or {}
    items = q.get("queue_running", []) + q.get("queue_pending", [])
    entries = [(k, v.get("prompt")) for k, v in (comfy("/history?max_items=200") or {}).items()]
    for real, item in [(i[1], i) for i in items] + entries:
        extra = item[3] if isinstance(item, list) and len(item) > 3 and isinstance(item[3], dict) else {}
        if real == pid or extra.get("comfy_agent_job") == pid:
            return real
    return None


def resolve_uncertain(j, job, tries=None):
    """Match an uncertain job against ComfyUI. Never sends it again."""
    tries = tries or int(os.environ.get("COMFY_AGENT_RESOLVE_TRIES", 10))
    answered = 0
    for _ in range(tries):
        try:
            real = find_in_comfy(job["id"])
        except Fail:
            time.sleep(1)
            continue
        if real:
            job.pop("prompt", None)
            job.pop("error", None)
            j.set_job(job, "queued", id=real)
            return True
        answered += 1
        if answered >= 3:
            break
        time.sleep(1)
    if answered:
        job.pop("prompt", None)
        j.set_job(job, "failed", error="ComfyUI may not have received this job: it is not in its queue or "
                                       "history. It was not sent again.")
        return False
    return None  # ComfyUI not answering: stays uncertain


def submit_all(j, op_id, stop=None):
    """Submit the operation's planned jobs in order, recording each one as soon as ComfyUI accepts it.
    A refused job stops the batch; the jobs after it are skipped. Returns the error text or None."""
    error = None
    for job in j.jobs(op_id):
        if job["state"] in ("submitting", "uncertain"):
            resolve_uncertain(j, job)
            continue
        if job["state"] != "planned":
            continue
        if error or (stop and stop()):
            job.pop("prompt", None)
            j.set_job(job, "skipped", error=error and "not sent: " + error or "not sent: cancelled")
            continue
        try:
            result = submit_job(j, job)
        except Fail as e:
            error = str(e).splitlines()[0]
            job.pop("prompt", None)
            j.set_job(job, "skipped", error="not sent: " + error)
            continue
        if result == "uncertain":
            resolve_uncertain(j, job)
        elif result == "refused":
            error = f"ComfyUI refused job {job['n']}/{job['of']}"
    if error:
        j.set_op(op_id, error=error)
    return error


def queue_jobs(recipe, pairs, count=1, raw_sets=()):
    """Unmanaged mode: build, record and submit in this process. Returns the accepted jobs."""
    jobs = build_jobs(recipe, pairs, count, raw_sets)
    j = journal()
    op_id = new_op_id()
    j.create_op(op_id, "inline", recipe.name, {}, jobs)
    submit_all(j, op_id)
    jobs = j.jobs(op_id)
    accepted = [x for x in jobs if x["state"] in ("queued", "uncertain", "submitting")]
    refused = [x for x in jobs if x["state"] == "failed"]
    if not accepted:
        j.set_op(op_id, state="failed")
        j.mark_delivered([x["rowid"] for x in jobs])
        raise Fail(refused[0]["error"] if refused else (jobs[0].get("error") or "Nothing was queued."))
    j.set_op(op_id, state="rendering")
    report_jobs(j, [x for x in jobs if x["state"] in JOB_FINAL])
    return accepted


# ---- waiting and collecting

def save_outputs(entry, job):
    items, seen = [], set()
    for node_out in (entry.get("outputs") or {}).values():
        for val in node_out.values():
            for it in val if isinstance(val, list) else []:
                if isinstance(it, dict) and it.get("filename"):
                    key = (it["filename"], it.get("subfolder", ""), it.get("type", "output"))
                    if key not in seen:
                        seen.add(key)
                        items.append(it)
    keep = [it for it in items if it.get("type") == "output"] or items
    folder = out_dir() / job["recipe"]
    saved = []
    for i, it in enumerate(keep, 1):
        q = urllib.parse.urlencode({"filename": it["filename"], "subfolder": it.get("subfolder", ""),
                                    "type": it.get("type", "output")})
        extra = f"-{i}" if len(keep) > 1 else ""
        dest = folder / f"{job['stamp']}-{job['n']:02d}{extra}{Path(it['filename']).suffix.lower()}"
        download(S["url"] + "/view?" + q, dest)
        saved.append(str(dest))
    job["remote"] = keep
    return saved


def live_ids():
    q = comfy("/queue") or {}
    return ({i[1] for i in q.get("queue_running", [])}, {i[1] for i in q.get("queue_pending", [])})


def poll_jobs(j, left, check_lost=False):
    """One look at ComfyUI for each job in left. Finished jobs are saved, recorded and removed from left."""
    for job in list(left):
        if job["state"] in ("uncertain", "submitting"):
            if resolve_uncertain(j, job, tries=1) is not True:
                if job["state"] == "failed":
                    left.remove(job)
                continue
        entry = (comfy(f"/history/{job['id']}") or {}).get(job["id"])
        if not entry:
            continue
        left.remove(job)
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            kinds = [m[0] for m in status.get("messages", [])]
            j.set_job(job, "cancelled" if "execution_interrupted" in kinds else "failed",
                      error=run_error(status), finished=time.time())
            continue
        files = save_outputs(entry, job)
        took = None
        times = [m[1].get("timestamp") for m in status.get("messages", []) if m[0] in ("execution_start", "execution_success")]
        if len(times) == 2 and all(times):
            took = (times[1] - times[0]) / 1000
        j.set_job(job, "done", files=files, took=took, finished=time.time())
    if check_lost and left:
        running, pending = live_ids()
        for job in [x for x in left if x["state"] == "queued" and x["id"] not in running | pending]:
            if not (comfy(f"/history/{job['id']}") or {}).get(job["id"]):
                left.remove(job)
                j.set_job(job, "lost", error="ComfyUI lost this job (was it restarted?)", finished=time.time())


def job_line(job):
    if job["state"] == "done":
        seed = f", seed {job['seed']}" if "seed" in job else ""
        took = f", {job['took']:.1f} s" if job.get("took") else ""
        return f"done {job['n']}/{job['of']}{seed}{took}: {', '.join(job.get('files') or []) or '(no files)'}"
    if job["state"] == "skipped":
        return f"skipped {job['n']}/{job['of']}: {job.get('error') or 'not sent'}"
    if job["state"] == "cancelled":
        return f"cancelled {job['n']}/{job['of']}"
    return f"FAILED {job['n']}/{job['of']}: {job.get('error')}"


def report_jobs(j, jobs):
    """Print each finished job once. Returns the ones printed now."""
    fresh = [x for x in jobs if x["state"] in JOB_FINAL and not x.get("delivered")]
    for job in fresh:
        print(job_line(job), flush=True)
        job["delivered"] = 1
    j.mark_delivered([x["rowid"] for x in fresh])
    return fresh


def remember_files(j, jobs):
    files = [f for x in jobs if x["state"] == "done" for f in x.get("files") or []]
    if files:
        j.set_meta("last", files)
        try:
            write_index()
        except OSError as e:
            print(f"(the gallery page wasn't updated: {e})", flush=True)
    return files


def wait_jobs(jobs, timeout=600):
    """Unmanaged mode: wait for jobs, saving each one's files as it finishes. Returns the jobs still running."""
    j = journal()
    start, last_check = time.time(), time.time()
    left = [x for x in jobs if x.get("state", "queued") not in JOB_FINAL]
    while left:
        check = time.time() - last_check > 15
        if check:
            last_check = time.time()
        poll_jobs(j, left, check_lost=check)
        report_jobs(j, jobs)
        if not left:
            break
        if time.time() - start > timeout:
            print(f"{len(left)} job(s) still running. Collect them later with: python3 comfy_agent.py wait", flush=True)
            break
        time.sleep(POLL)
    for op_id in {x["op"] for x in jobs if x.get("op")}:
        if not j.jobs(op_id, states=("planned", "submitting", "uncertain", "queued")):
            j.set_op(op_id, state="done")
    remember_files(j, [x for x in jobs if x not in left])
    return left


def cancel_jobs(j, jobs, timeout=60):
    """Take our jobs out of ComfyUI: delete the waiting ones, interrupt the running one, and confirm
    in the queue that none is left. Jobs not sent yet are skipped."""
    for job in [x for x in jobs if x["state"] == "planned"]:
        job.pop("prompt", None)
        j.set_job(job, "skipped", error="not sent: cancelled")
    mine = [x for x in jobs if x["state"] in ("queued", "uncertain", "submitting") and x.get("id")]
    ids = {x["id"] for x in mine}
    start = time.time()
    while ids and time.time() - start < timeout:
        running, pending = live_ids()
        if not ids & (running | pending):
            break
        if ids & pending:
            comfy("/queue", {"delete": sorted(ids & pending)})
        for pid in ids & running:
            comfy("/interrupt", {"prompt_id": pid})
        time.sleep(min(POLL, 0.5))
    else:
        if ids:
            raise Fail("ComfyUI still lists the cancelled jobs after a minute.")
    left = list(mine)
    poll_jobs(j, left)  # a job that finished in the meantime keeps its files
    for job in left:
        j.set_job(job, "cancelled", finished=time.time())


def free_vram():
    comfy("/free", {"unload_models": True, "free_memory": True})
    print("Asked ComfyUI to unload its models and free the GPU.")


# ---------------------------------------------------------------- managed mode: the runner

class Blocked(Exception):
    """The model stays unloaded; the next operation or reconcile restores it."""


def comfy_free_mb():
    try:
        return free_mb(comfy("/system_stats", timeout=10), inference_cfg().get("device", 0))
    except (Fail, ComfyError):
        return None


def comfy_busy():
    running, pending = live_ids()
    return bool(running or pending)


class Runner:
    """Owns one managed operation from the lease to the restore. Runs detached (comfy_agent.py _runner)."""

    def __init__(self, op_id):
        self.j = journal()
        self.id = op_id
        self.op = self.j.op(op_id)
        self.data = self.op["data"]
        self.cfg = self.data.get("inference") or inference_cfg()
        self.adapter = make_adapter(self.cfg)
        self.lease = Lease(self.cfg.get("lease_dir") or lease_dir())
        self._guard_at = 0.0
        self._stop = threading.Event()

    def event(self, text):
        print(text, flush=True)
        self.j.append(self.id, "events", [time.time(), text])

    def state(self, state, **kw):
        if state in OP_FINAL and self.lease.fd is not None:
            self._final = dict(state=state, **kw)  # written after the lease is released, so "done" means free
            return
        self.j.set_op(self.id, state=state, **kw)

    def cancelled(self):
        return bool(self.j.op(self.id)["cancel"])

    def timed(self, phase, start):
        self.j.set_op(self.id, data={"phases": {**self.j.op(self.id)["data"].get("phases", {}),
                                                phase: round(time.time() - start, 2)}})

    def vram(self, key):
        mb = comfy_free_mb()
        self.j.set_op(self.id, data={"vram_mb": {**self.j.op(self.id)["data"].get("vram_mb", {}), key: mb}})
        return mb

    def waiting(self, holder):
        holder = holder or {}
        where = ""
        if holder.get("out") and Path(holder["out"]) != out_dir():
            where = f" (output folder {holder['out']})"
        self.event(f"waiting for the GPU: operation {holder.get('op', '(unknown)')}{where} has it")

    def guard(self, force=False):
        """While we hold the lease the model must stay off the card. With the llama.cpp router a request that
        asks for ?autoload=true still loads it, and an older llama-swap loads on any request. Catch that and
        unload again. This shortens the overlap; it can't prevent it."""
        if self.adapter.kind not in ("llama-router", "llama-swap") or not (force or time.time() - self._guard_at > 2):
            return
        self._guard_at = time.time()
        try:
            back = self.adapter.loaded()
        except AdapterError:
            return
        if not back:
            return
        self.event(f"a request loaded the language model ({', '.join(back)}) while ComfyUI had the GPU; "
                   "unloaded it again")
        self.j.append(self.id, "notes", f"intrusion: {', '.join(back)} loaded during the lease")
        self.adapter.unload(back)

    def note_once(self, text):
        if text not in self.j.op(self.id)["data"].get("notes", []):
            self.j.append(self.id, "notes", text)

    def names(self, models):
        if self.adapter.kind == "command" and not self.cfg.get("model"):
            return ""
        return f" ({', '.join(models)})"

    def beat(self):
        while not self._stop.wait(1):
            with contextlib.suppress(Exception):
                self.j.heartbeat(self.id)

    def run(self):
        self.j.heartbeat(self.id, os.getpid())
        threading.Thread(target=self.beat, daemon=True).start()
        held = False
        try:
            t = time.time()
            held = self.lease.acquire({"op": self.id, "out": str(out_dir())}, poll=0.2, should_stop=self.cancelled,
                                      on_wait=self.waiting)
            self.op = self.j.op(self.id)
            if self.op["state"] in ("done", "failed", "cancelled"):
                return
            if not held:
                cancel_jobs(self.j, self.j.jobs(self.id))
                self.state("cancelled")
                self.event("cancelled before it started")
                return
            self.timed("lease", t)
            if not self.op["data"].get("unloaded"):
                if self.cancelled():
                    cancel_jobs(self.j, self.j.jobs(self.id))
                    self.state("cancelled")
                    self.event("cancelled before it started")
                    return
                self.unload()
            self.state("submitting")
            submit_all(self.j, self.id, stop=self.cancelled)
            self.state("rendering")
            self.render()
            self.hand_back()
        except Blocked as e:
            self.state("blocked", error=str(e))
            self.event(f"BLOCKED: {e} The language model stays unloaded. When that's fixed: "
                       "python3 comfy_agent.py reconcile")
        except Exception as e:  # anything else: leave a clear record
            msg = str(e).splitlines()[0] if str(e) else e.__class__.__name__
            if self.j.op(self.id)["data"].get("unloaded"):
                self.state("blocked", error=msg)
                self.event(f"BLOCKED: {msg} The language model stays unloaded. When that's fixed: "
                           "python3 comfy_agent.py reconcile")
            else:
                for job in self.j.jobs(self.id, states=("planned",)):
                    job.pop("prompt", None)
                    self.j.set_job(job, "skipped", error="not sent: " + msg)
                self.state("failed", error=msg)
                self.event(f"FAILED: {msg}")
        finally:
            if held:
                self.lease.release()
            final = getattr(self, "_final", None)
            if final:
                self.j.set_op(self.id, **final)
            self._stop.set()

    def unload(self):
        t = time.time()
        self.state("unloading")
        before = self.vram("before_unload")
        models = self.adapter.loaded()
        if models:
            self.adapter.unload(models)
            limit = time.time() + float(self.cfg.get("unload_timeout", 120))
            while self.adapter.is_unloaded(models) is False:
                if time.time() > limit:
                    raise AdapterError(f"the language model ({', '.join(models)}) didn't leave the GPU")
                time.sleep(0.25)
        after = self.vram("after_unload")
        freed = after - before if models and before is not None and after is not None and after > before else None
        self.j.set_op(self.id, data={"unloaded": True, "models": models, "freed_mb": freed,
                                     "adapter": self.adapter.kind})
        self.timed("unload", t)
        if models:
            self.event(f"unloaded the language model{self.names(models)}" +
                       (f", {freed / 1024:.1f} GB freed" if freed else ""))

    def render(self):
        t = time.time()
        left = self.j.jobs(self.id, states=("queued", "uncertain", "submitting"))
        last_check, down_since = time.time(), None
        while left:
            if self.cancelled():
                cancel_jobs(self.j, left)
                break
            try:
                check = time.time() - last_check > 15
                if check:
                    last_check = time.time()
                poll_jobs(self.j, left, check_lost=check)
                down_since = None
                self.guard()
            except Fail:
                down_since = down_since or time.time()
                if time.time() - down_since > float(self.cfg.get("down_timeout", 120)):
                    raise Blocked("ComfyUI stopped answering during the render.") from None
            if left:
                time.sleep(POLL)
        jobs = self.j.jobs(self.id)
        files = remember_files(self.j, jobs)
        send = self.j.op(self.id)["data"].get("send")
        if files and send:
            try:
                self.event(send_files(files, send))
            except Exception as e:  # delivery never fails the render
                self.event(f"Sending failed (the files are saved): {e}")
                self.j.append(self.id, "notes", f"send failed: {e}")
        self.timed("render", t)

    def hand_back(self):
        data = self.j.op(self.id)["data"]
        models = list(data.get("models") or [])
        need = data.get("freed_mb") or 0
        inherited = [o for o in self.j.blocked_ops() if o["id"] != self.id]
        for o in inherited:
            models += [m for m in o["data"].get("models") or [] if m not in models]
            need += o["data"].get("freed_mb") or 0
        need = int(self.cfg["need_mb"]) if self.cfg.get("need_mb") else need
        while True:
            t = time.time()
            self.state("idle-wait")
            limit = time.time() + float(self.cfg.get("idle_timeout", 3600))
            told = False
            while comfy_busy():
                self.guard()
                if not told:
                    self.event("waiting for ComfyUI's queue to empty before the language model comes back")
                    told = True
                if time.time() > limit:
                    raise Blocked("ComfyUI is still busy with other jobs.")
                time.sleep(POLL)
            self.timed("idle", t)
            t = time.time()
            self.state("freeing")
            free_vram()
            if models and not need:
                self.note_once("no free-memory check: the unload freed nothing ComfyUI could see (another GPU, "
                               "unified memory, or a GPU ComfyUI can't measure). Set inference.need_mb to check anyway")
            if models and need:
                want = need + int(self.cfg.get("margin_mb", 512))
                limit = time.time() + float(self.cfg.get("free_timeout", 180))
                while True:
                    self.guard()
                    have = comfy_free_mb()
                    if have is None:
                        self.note_once("no free-memory check: ComfyUI reports no GPU memory figure for this device")
                        break
                    if have >= want:
                        break
                    if time.time() > limit:
                        self.vram("after_free")
                        raise Blocked(f"memory not released ({have / 1024:.1f} GB free, need {want / 1024:.1f} GB).")
                    time.sleep(0.5)
            self.vram("after_free")
            self.timed("free", t)
            if not comfy_busy():  # nothing slipped in while memory was being freed
                break
        if not models:
            self.state("cancelled" if self.cancelled() else "done")
            return
        t = time.time()
        self.state("restoring")
        try:
            self.adapter.load(models)
            limit = time.time() + float(self.cfg.get("load_timeout", 300))
            while not self.adapter.is_ready(models):
                if time.time() > limit:
                    raise AdapterError("it didn't report ready in time")
                time.sleep(0.5)
        except AdapterError as e:
            raise Blocked(f"the language model didn't load back ({e}).") from None
        self.timed("restore", t)
        for o in inherited:
            self.j.set_op(o["id"], state="done", data={"restored_by": self.id})
        self.state("cancelled" if self.cancelled() else "done")
        if self.adapter.kind == "command" and not self.cfg.get("load"):
            self.event("the language model loads back on the next request")
        else:
            self.event(f"restored the language model{self.names(models)}")


def spawn_runner(op_id):
    logs = out_dir() / ".runs"
    logs.mkdir(parents=True, exist_ok=True)
    kw = {"stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT, "cwd": str(KIT), "close_fds": True}
    cmd = [sys.executable, str(Path(__file__).resolve()), "_runner", "--out", str(out_dir()), op_id]
    with open(logs / f"{op_id}.log", "ab") as log:
        if os.name == "nt":
            # Its own process group, no console, and out of the client's job object when that is allowed, so an
            # MCP client that kills its server's process tree doesn't kill the runner.
            detached = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            try:
                p = subprocess.Popen(cmd, stdout=log, creationflags=detached | 0x01000000, **kw)  # BREAKAWAY_FROM_JOB
            except OSError:
                p = subprocess.Popen(cmd, stdout=log, creationflags=detached, **kw)
        else:
            p = subprocess.Popen(cmd, stdout=log, start_new_session=True, **kw)
    _children.append(p)
    journal().set_op(op_id, pid=p.pid, heartbeat=time.time())
    return p


def reap():
    for p in list(_children):
        if p.poll() is not None:
            _children.remove(p)


def runner_dead(op):
    stale = time.time() - (op["heartbeat"] or 0) > STALE
    return stale or (op["pid"] and not pid_alive(op["pid"]))


def reconcile(include_blocked=False, only=None, quiet=True):
    """Start a new runner for every unfinished managed operation whose runner died. With include_blocked,
    also retry the blocked ones (they wait for memory or ComfyUI). Returns the operations restarted."""
    reap()
    j = journal()
    todo = [o for o in j.ops(limit=50, unfinished=True) if o["mode"] == "managed" and runner_dead(o)]
    if include_blocked:
        todo += j.blocked_ops()
    started = []
    for op in todo:
        if only and op["id"] not in only:
            continue
        if not j.claim(op["id"], op["heartbeat"]):
            continue
        if op["state"] == "blocked":
            j.set_op(op["id"], state="queued", error=None)
        spawn_runner(op["id"])
        started.append(op["id"])
        if not quiet:
            print(f"restarted the runner for operation {op['id']} ({op['state']})", flush=True)
    return started


def start_managed(recipe, pairs, count=1, raw_sets=(), send=None):
    jobs = build_jobs(recipe, pairs, count, raw_sets)
    op_id = new_op_id()
    data = {"settings": {"url": S["url"], "out": S["out"], "send": S.get("send", {})},
            "inference": {**inference_cfg(), "lease_dir": str(lease_dir())}, "send": send, "events": [], "phases": {}, "vram_mb": {}}
    j = journal()
    j.create_op(op_id, "managed", recipe.name, data, jobs)
    j.set_op(op_id, heartbeat=time.time())
    print(f"queued {len(jobs)} job(s) of {recipe.name} as operation {op_id}", flush=True)
    spawn_runner(op_id)
    return op_id


def follow(op_ids, timeout, stop=None, progress=None, all_events=True):
    """Print an operation's lines and finished jobs as they happen, until it ends or timeout.
    Returns (operations, jobs reported now)."""
    j = journal()
    start = time.time()
    seen = {i: 0 if all_events else len((j.op(i) or {"data": {}})["data"].get("events", [])) for i in op_ids}
    reported, last_reconcile, last_progress, pings = [], time.time(), time.time(), 0
    while True:
        ops = [j.op(i) for i in op_ids]
        lines = []
        for op in ops:
            ev = op["data"].get("events", [])
            lines += [(t, text, None) for t, text in ev[seen[op["id"]]:]]
            seen[op["id"]] = len(ev)
        jobs = [x for i in op_ids for x in j.jobs(i)]
        fresh = [x for x in jobs if x["state"] in JOB_FINAL and not x.get("delivered")]
        lines += [(x.get("finished") or time.time(), None, x) for x in fresh]
        for _, text, job in sorted(lines, key=lambda l: l[0]):
            print(text if job is None else job_line(job), flush=True)
        j.mark_delivered([x["rowid"] for x in fresh])
        reported += fresh
        if all(o["state"] in OP_FINAL for o in ops):
            break
        if (stop and stop()) or time.time() - start > timeout:
            for o in ops:
                if o["state"] not in OP_FINAL:
                    js = j.jobs(o["id"])
                    done = sum(x["state"] in JOB_FINAL for x in js)
                    print(f"still rendering: operation {o['id']}, {done} of {len(js)} done. "
                          "Collect with: python3 comfy_agent.py wait", flush=True)
            break
        if time.time() - last_reconcile > 5:
            last_reconcile = time.time()
            reconcile(only=op_ids)
        if progress and time.time() - last_progress > float(os.environ.get("COMFY_AGENT_PROGRESS_EVERY", 10)):
            last_progress = time.time()
            pings += 1
            done = sum(x["state"] in JOB_FINAL for x in jobs)
            progress(pings, f"{done} of {len(jobs)} done, {ops[0]['state']}")
        time.sleep(0.3)
    return ops, reported


def undelivered_ops(mode="managed"):
    j = journal()
    ids = [o["id"] for o in j.ops(limit=50, unfinished=True) if o["mode"] == mode]
    for x in j.jobs(undelivered=True):
        if x["op"] not in ids and (j.op(x["op"]) or {}).get("mode") == mode:
            ids.append(x["op"])
    return ids


def cmd_run(a):
    recipe = get_recipe(a.recipe)
    pairs, sets = parse_pairs(a.values), parse_pairs(a.set or [])
    if managed():
        reconcile()
        op_id = start_managed(recipe, pairs, max(1, a.count), sets, send=a.send)
        if a.no_wait and inference_cfg()["type"] == "llama-router":
            print("The runner finishes it and brings the language model back. Collect with: python3 comfy_agent.py wait")
            return
        if a.no_wait:
            print("Waiting anyway: the language model is unloaded, and it must not load back until these are done.")
        ops, _ = follow([op_id], a.timeout)
        end_status(ops)
        return
    jobs = queue_jobs(recipe, pairs, max(1, a.count), sets)
    print(f"queued {len(jobs)} job(s) of {recipe.name}", flush=True)
    if a.no_wait:
        print("Collect the results with: python3 comfy_agent.py wait")
        return
    left = wait_jobs(jobs, a.timeout)
    finish(jobs, left, a)
    err = journal().op(jobs[0]["op"])["error"]
    if err:
        raise Fail(err)


def end_status(ops):
    bad = [o for o in ops if o["state"] in ("failed", "blocked") or (o["state"] == "done" and o["error"])]
    if bad:
        raise Fail("")


def cmd_wait(a):
    if managed():
        reconcile()
    ids = undelivered_ops("managed")
    pending = load_state().get("pending", [])
    j = journal()
    old = [x for x in j.jobs(undelivered=True) if x["state"] in JOB_FINAL and x["op"] not in ids]
    if not ids and not pending and not old:
        print("No jobs are waiting.")
        return
    report_jobs(j, old)
    files = []
    if ids:
        ops, done = follow(ids, a.timeout)
        files = [f for x in done if x["state"] == "done" for f in x.get("files") or []]
    if pending:
        print(f"waiting for {len(pending)} job(s)", flush=True)
        left = wait_jobs(pending, a.timeout)
        finish(pending, left, a)
    elif files and a.send:
        try:
            print(send_files(files, a.send))
        except Fail as e:
            print(f"Sending failed (the files are saved): {e}")


def finish(jobs, left, a):
    files = [f for x in jobs if x not in left for f in x.get("files") or []]
    if files and a.send:
        try:
            print(send_files(files, a.send))
        except Fail as e:
            print(f"Sending failed (the files are saved): {e}")
    if a.free and not left:
        free_vram()


def cmd_ops(a):
    j = journal()
    reconcile()
    ops = [j.op(a.op)] if a.op else j.ops(limit=a.limit)
    if a.op and not ops[0]:
        raise Fail(f"No operation {a.op}.")
    if a.json:
        out = []
        for o in ops:
            d = o["data"]
            out.append({"id": o["id"], "state": o["state"], "mode": o["mode"], "recipe": o["recipe"],
                        "error": o["error"], "created": o["created"], "adapter": d.get("adapter"),
                        "models": d.get("models"), "phases": d.get("phases", {}), "vram_mb": d.get("vram_mb", {}),
                        "notes": d.get("notes", []), "events": [e[1] for e in d.get("events", [])],
                        "jobs": [{k: x.get(k) for k in ("n", "state", "id", "seed", "files", "error", "took")}
                                 for x in j.jobs(o["id"])]})
        for item in out:
            for job in item["jobs"]:
                job["prompt_id"] = job.pop("id")
        print(json.dumps(out if not a.op else out[0], indent=1))
        return
    if not ops:
        print("No operations yet.")
    for o in ops:
        js = j.jobs(o["id"])
        done = sum(x["state"] == "done" for x in js)
        err = f": {o['error']}" if o["error"] else ""
        print(f"{o['id']} {o['state']} {o['recipe']} {done}/{len(js)} done{err}")


def cmd_cancel(a):
    j = journal()
    ops = [j.op(a.op)] if a.op else j.ops(limit=50, unfinished=True)
    if a.op and not ops[0]:
        raise Fail(f"No operation {a.op}.")
    if not ops:
        print("Nothing to cancel.")
        return
    for o in ops:
        if o["state"] in OP_FINAL:
            print(f"operation {o['id']} already ended ({o['state']})")
            continue
        j.set_op(o["id"], cancel=1)
        if o["mode"] == "managed":
            print(f"cancelling operation {o['id']}", flush=True)
        else:
            cancel_jobs(j, j.jobs(o["id"]))
            j.set_op(o["id"], state="cancelled")
            print(f"cancelled operation {o['id']}", flush=True)
    if managed():
        reconcile()


def cmd_reconcile(a):
    started = reconcile(include_blocked=True, quiet=False)
    if not started:
        print("Nothing to reconcile: no unfinished or blocked operations.")
        return
    if a.no_wait:
        return
    ops, _ = follow(started, a.timeout, all_events=False)
    end_status(ops)


def cmd__runner(a):
    S["out"] = a.out
    op = journal().op(a.op)
    if not op:
        raise Fail(f"No operation {a.op}.")
    snap = op["data"].get("settings") or {}
    S.update({k: v for k, v in snap.items() if v is not None})
    S["inference"] = op["data"].get("inference") or {}
    S.pop("unload_llm", None)
    Runner(a.op).run()


# ---------------------------------------------------------------- send and gallery

def send_files(files, target):
    dest = S.get("send", {}).get(target, target)
    if dest.startswith("tailscale:"):
        device = dest.split(":", 1)[1]
        exe = shutil.which("tailscale")
        if not exe:
            raise Fail("Tailscale is not installed here, so Taildrop can't send. https://tailscale.com/download")
        try:
            r = subprocess.run([exe, "file", "cp", *files, device + ":"], capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise Fail(f"Taildrop to {device} timed out. Is the phone on, with Tailscale open?") from None
        if r.returncode:
            raise Fail(f"Taildrop to {device} failed: {(r.stderr or r.stdout).strip()[:300]}")
        return f"Sent {len(files)} file(s) to {device} with Taildrop. On an iPhone, open the Tailscale app to save them."
    if dest.startswith("ntfy:"):
        topic = dest[5:]
        url = topic if topic.startswith("http") else "https://ntfy.sh/" + topic
        for f in files:
            with open(f, "rb") as fh:
                open_url(url, "PUT", fh.read(), {"Filename": Path(f).name, "Title": "ComfyUI: " + Path(f).parent.name},
                         timeout=300).close()
        return f"Sent {len(files)} file(s) to ntfy topic {url}."
    raise Fail(f"Don't know where '{target}' is. Set it once: python3 comfy_agent.py config phone=tailscale:DEVICE "
               "(or phone=ntfy:TOPIC). Your Tailscale devices: tailscale status")


def cmd_send(a):
    files = a.files or ["last"]
    if files == ["last"]:
        files = load_state().get("last", [])
        if not files:
            raise Fail("Nothing made yet to send.")
    missing = [f for f in files if not Path(f).is_file()]
    if missing:
        raise Fail(f"Can't find {missing[0]}")
    print(send_files([str(Path(f).resolve()) for f in files], a.to))


def write_index():
    root = out_dir()
    media = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXT + VIDEO_EXT]
    media.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    tiles = []
    for p in media[:300]:
        rel = html.escape(urllib.parse.quote(p.relative_to(root).as_posix()))
        cap = html.escape(p.relative_to(root).as_posix())
        tag = (f'<video src="{rel}" controls playsinline preload="metadata"></video>' if p.suffix.lower() in VIDEO_EXT
               else f'<img src="{rel}" loading="lazy" alt="{cap}">')
        tiles.append(f'<figure><a href="{rel}">{tag}</a><figcaption>{cap}</figcaption></figure>')
    page = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>ComfyUI results</title><style>body{margin:0;background:#111;color:#bbb;font:13px system-ui}"
            "main{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:6px;padding:6px}"
            "figure{margin:0}img,video{width:100%;display:block;border-radius:4px}figcaption{padding:2px;"
            "overflow:hidden;text-overflow:ellipsis;white-space:nowrap}</style><main>" + "".join(tiles) + "</main>")
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(page, encoding="utf-8")


def gallery_server(port, prefix):
    """outputs/ over HTTP on 127.0.0.1, with byte ranges so phones can play the videos."""
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kw):
            super().__init__(*args, directory=str(out_dir()), **kw)

        def translate_path(self, path):
            if path == prefix or path.startswith(prefix + "/"):
                path = path[len(prefix):] or "/"
            return super().translate_path(path)

        def send_head(self):
            self.left = None
            if any(part.startswith(".") for part in urllib.parse.urlparse(self.path).path.split("/")):
                self.send_error(404)
                return None
            path = self.translate_path(self.path)
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            if not m or not (m[1] or m[2]) or not os.path.isfile(path):
                return super().send_head()
            size = os.path.getsize(path)
            start = int(m[1]) if m[1] else max(0, size - int(m[2]))
            end = min(int(m[2]), size - 1) if m[1] and m[2] else size - 1
            if start >= size or start > end:
                self.send_error(416)
                return None
            f = open(path, "rb")
            f.seek(start)
            self.send_response(206)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.left = end - start + 1
            return f

        def copyfile(self, source, outputfile):
            if self.left is None:
                return super().copyfile(source, outputfile)
            while self.left > 0:
                chunk = source.read(min(1 << 16, self.left))
                if not chunk:
                    break
                outputfile.write(chunk)
                self.left -= len(chunk)

        def log_message(self, *a):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def cmd_gallery(a):
    path = "/" + S.get("gallery_path", "comfy-agent").strip("/")
    port = int(S.get("gallery_port", 8766))
    if a.serve:
        gallery_server(port, path).serve_forever()
        return
    exe = shutil.which("tailscale")
    if not exe:
        raise Fail("The gallery uses Tailscale Serve, and Tailscale is not installed here.")
    pid_file = out_dir() / ".gallery.pid"
    if a.off:
        subprocess.run([exe, "serve", "--bg", f"--set-path={path}", "off"], check=False)
        if pid_file.exists():
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(pid_file.read_text()), 15)
            pid_file.unlink()
        print("The gallery is off.")
        return
    write_index()
    local = f"http://127.0.0.1:{port}"
    try:
        open_url(local + "/index.html", timeout=2).close()
    except (urllib.error.URLError, OSError):
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "gallery", "--serve"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        pid_file.write_text(str(proc.pid))
        time.sleep(1)
    r = subprocess.run([exe, "serve", "--bg", f"--set-path={path}", local], capture_output=True, text=True)
    if r.returncode:
        raise Fail("tailscale serve failed: " + (r.stderr or r.stdout).strip()[:400])
    me = json.loads(subprocess.run([exe, "status", "--json"], capture_output=True, text=True).stdout)["Self"]
    print(f"Gallery: https://{me['DNSName'].rstrip('.')}{path}/  (only devices on your tailnet can open it)")


# ---------------------------------------------------------------- list, show, doctor, config

def cmd_list(a):
    found = all_recipes()
    if not found:
        print("No recipes yet. Make one from a workflow: python3 comfy_agent.py add WORKFLOW.json --name NAME")
        return
    for name, folder in found.items():
        r = Recipe(folder)
        settings = ", ".join(k + ("*" if v.get("required") else "") for k, v in r.inputs.items())
        print(f"{name}: {r.card.get('title', '')} [{settings}]")
    print("* = required. Details: python3 comfy_agent.py show RECIPE")


def cmd_show(a):
    r = get_recipe(a.recipe)
    c = r.card
    print(f"{r.name}: {c.get('title', '')}")
    if c.get("does"):
        print(c["does"])
    print("Settings (name=value):")
    for key, spec in r.inputs.items():
        kind = kind_of(key, spec)
        bits = [kind]
        d = spec.get("default")
        if spec.get("required"):
            bits.append("required")
        elif kind in MEDIA_KINDS:
            bits.append("a file path; leave out to use the recipe's example" if d else "a file path")
        elif d not in (None, ""):
            text = json.dumps(d) if not isinstance(d, str) else d
            bits.append("default " + (text if len(text) <= 70 else text[:67] + "..."))
        if spec.get("choices"):
            bits.append("one of " + ", ".join(map(str, spec["choices"])))
        print(f"  {key}: {spec.get('help', '')} ({'; '.join(bits)})")
    for tip in c.get("tips", []):
        print("Tip:", tip)
    notes = r.folder / "notes.md"
    if a.notes and notes.exists():
        print("\n" + notes.read_text(encoding="utf-8")[:4000])
    elif notes.exists():
        print("The workflow author's notes: python3 comfy_agent.py show", r.name, "--notes")


def cmd_doctor(a):
    st = comfy("/system_stats", timeout=10)
    print(f"ComfyUI {st['system'].get('comfyui_version', '?')} is up at {S['url']}.")
    for dev in st.get("devices", [])[:2]:
        name = dev["name"].split(":")[1].strip() if ":" in dev["name"] else dev["name"]
        print(f"GPU: {name}, {gb(dev.get('vram_free'))} free of {gb(dev.get('vram_total'))}.")
    q = comfy("/queue")
    print(f"Queue: {len(q['queue_running'])} running, {len(q['queue_pending'])} waiting.")
    for name, folder in all_recipes().items():
        r = Recipe(folder)
        nodes, models = preflight(r.prompt())
        print(f"  {name}: " + ("ready" if not (nodes or models) else
                               f"not ready ({len(nodes)} node types, {len(models)} models missing). "
                               f"python3 comfy_agent.py install {name}"))
    targets_ = S.get("send", {})
    print("Send to: " + (", ".join(f"{k} = {v}" for k, v in targets_.items()) or
                         "nothing set up (python3 comfy_agent.py config phone=tailscale:DEVICE)"))
    pending = load_state().get("pending", [])
    if pending:
        print(f"{len(pending)} queued job(s) not collected yet: python3 comfy_agent.py wait")
    cfg = inference_cfg()
    if cfg["type"] == "none":
        print("Local model handoff: off (no inference configured).")
        return
    reconcile()
    print("Local model: " + make_adapter(cfg).describe())
    lease = Lease(lease_dir())
    holder = lease.holder() if lease.held_by_anyone() else None
    print("GPU lease: " + (f"held by operation {holder.get('op')}" if holder else "free"))
    j = journal()
    for o in j.ops(limit=50, unfinished=True) + j.blocked_ops():
        print(f"  operation {o['id']}: {o['state']}" + (f" ({o['error']})" if o["error"] else ""))
    if j.blocked_ops():
        print("Blocked operations leave the language model unloaded. Fix the cause, then: python3 comfy_agent.py reconcile")


def cmd_config(a):
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    for k, v in parse_pairs(a.values):
        if k.startswith("inference."):
            sub = k.split(".", 1)[1]
            inf = cfg.setdefault("inference", {})
            if v == "":
                inf.pop(sub, None)
            else:
                inf[sub] = json_or_text(v) if sub not in ("model", "api_key", "unload", "load", "ready") else v
            if sub == "type":
                from comfy_lifecycle import ADAPTERS
                if v and v not in ADAPTERS:
                    raise Fail(f"inference.type must be one of {', '.join(ADAPTERS)}")
        elif k in ("url", "out", "gallery_path", "gallery_port", "unload_llm", "mcp_wait"):
            cfg[k] = v
        else:
            cfg.setdefault("send", {})[k] = v
    if a.values:
        CONFIG.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    print(json.dumps({"url": S["url"], "out": S["out"], **cfg}, indent=1))


# ---------------------------------------------------------------- install and restart

def folder_paths():
    try:
        return comfy("/internal/folder_paths")
    except ComfyError:
        raise Fail("This ComfyUI doesn't say where its folders are (no /internal/folder_paths). "
                   "Install by hand from the list above.") from None


def resolve_model_url(m):
    url = m.get("url") or ""
    file = m["file"]
    if not url:
        return None
    if "huggingface.co" in url:
        url = url.replace("/blob/", "/resolve/")
        if "/resolve/" in url:
            return url if url.rstrip("/").endswith(file) else url.rstrip("/") + "/" + file
        repo = "/".join(urllib.parse.urlparse(url).path.strip("/").split("/")[:2])
        try:
            with open_url(f"https://huggingface.co/api/models/{repo}", headers=auth_headers(url), timeout=30) as r:
                files = [s["rfilename"] for s in json.load(r).get("siblings", [])]
        except (urllib.error.URLError, OSError, ValueError):
            return None
        hit = [f for f in files if base_name(f) == file]
        return f"https://huggingface.co/{repo}/resolve/main/{hit[0]}" if hit else None
    civ = re.search(r"civitai\.com/models/\d+.*?modelVersionId=(\d+)", url)
    if civ:
        return f"https://civitai.com/api/download/models/{civ.group(1)}"
    return url


def remote_size(url):
    try:
        with open_url(url, headers={**auth_headers(url), "Range": "bytes=0-0"}, timeout=30) as r:
            cr = r.headers.get("Content-Range", "")
            if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                return int(cr.rsplit("/", 1)[1])
            return int(r.headers["Content-Length"]) if r.headers.get("Content-Length") else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def comfy_python(comfy_dir):
    if os.environ.get("COMFY_PYTHON"):
        return os.environ["COMFY_PYTHON"]
    for c in (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe", "venv/Scripts/python.exe",
              "../python_embeded/python.exe", "../python_embedded/python.exe"):
        p = (comfy_dir / c).resolve()
        if p.exists():
            return str(p)
    raise Fail("Can't find ComfyUI's own Python. Tell me where it is: COMFY_PYTHON=/path/to/python python3 comfy_agent.py install ...")


def install_pack(pack, custom_nodes):
    comfy_dir = custom_nodes.parent
    if shutil.which("comfy") and pack.get("id"):
        cmd = ["comfy", "--workspace", str(comfy_dir), "--skip-prompt", "node", "install", pack["id"]]
        print("  $", " ".join(cmd), flush=True)
        if subprocess.run(cmd).returncode == 0:
            return
        print("  comfy-cli could not install it; trying git instead.", flush=True)
    repo = pack.get("repo")
    if not repo and pack.get("id"):
        with open_url(f"https://api.comfy.org/nodes/{pack['id']}", timeout=30) as r:
            repo = json.load(r).get("repository")
    if not repo:
        raise Fail(f"No download address for node pack {pack.get('id')}.")
    name = re.split(r"[\\/]", repo.rstrip("/\\"))[-1]
    dest = custom_nodes / (name[:-4] if name.endswith(".git") else name)
    if not dest.exists():
        subprocess.run(["git", "clone", "--depth", "1", repo, str(dest)], check=True)
    if (dest / "requirements.txt").exists():
        subprocess.run([comfy_python(comfy_dir), "-m", "pip", "install", "-r", str(dest / "requirements.txt")], check=True)
    if (dest / "install.py").exists():
        subprocess.run([comfy_python(comfy_dir), "install.py"], cwd=dest, check=True)


def cmd_install(a):
    r = get_recipe(a.recipe)
    nodes, models = preflight(r.prompt())
    if not nodes and not models:
        print(f"{r.name} is ready: everything it needs is installed.")
        return
    needs = r.card.get("needs", {})
    packs = [p for p in needs.get("nodes", []) if set(p.get("classes", [])) & set(nodes)]
    unknown_nodes = [n for n in nodes if not any(n in p.get("classes", []) for p in packs)]
    by_file = {m["file"]: m for m in needs.get("models", [])}
    paths = folder_paths()
    custom_nodes = Path(paths["custom_nodes"][0])
    plan, total, todo, unknown_models = [], 0, [], []
    for p in packs:
        plan.append(f"node pack {p.get('id')} from {p.get('repo') or 'the Comfy registry'} "
                    f"(code from the internet, goes in {custom_nodes})")
    for file in models:
        m = by_file.get(file)
        url = resolve_model_url(m) if m else None
        if not m or not url or m.get("folder") not in paths:
            unknown_models.append(file)
            continue
        size = m.get("size") or remote_size(url)
        total += size or 0
        dest = Path(paths[m["folder"]][0]) / m.get("subfolder", "") / file
        todo.append((url, dest))
        plan.append(f"model {file}, {gb(size)}, into {dest.parent}, from {url}")
    print(f"To install for {r.name}:")
    for line in plan:
        print("  " + line)
    for n in unknown_nodes:
        print(f"  ?? node type {n}: the recipe doesn't say which pack has it. Search ComfyUI-Manager for it.")
    for f in unknown_models:
        print(f"  ?? model {f}: the recipe has no download link for it. Find it by name (Hugging Face, Civitai).")
    if todo:
        print(f"Download total: {gb(total)}")
    if not a.yes:
        print(f"Nothing installed yet. Show this list to the person. With their OK: python3 comfy_agent.py install {r.name} --yes")
        return
    if not custom_nodes.exists():
        raise Fail("ComfyUI's folders are not on this computer. Run install on the computer that runs ComfyUI.")
    for p in packs:
        print(f"Installing node pack {p.get('id')}...", flush=True)
        install_pack(p, custom_nodes)
    for url, dest in todo:
        print(f"Downloading {dest.name}...", flush=True)
        download(url, dest, auth_headers(url), quiet=False)
    if packs:
        print("New node packs load when ComfyUI starts. Restart it: python3 comfy_agent.py restart")
    else:
        print(f"Done. Check with: python3 comfy_agent.py doctor")


def cmd_restart(a):
    asked = False
    for path, method in (("/v2/manager/reboot", "POST"), ("/api/manager/reboot", "POST"),
                         ("/api/manager/reboot", "GET"), ("/manager/reboot", "GET")):
        try:
            comfy(path, method=method, timeout=10)
            asked = True
            break
        except ComfyError as e:
            if e.status == 403:
                raise Fail("ComfyUI-Manager refused to restart (its security level). Restart ComfyUI by hand.") from None
        except Fail:
            asked = True
            break
    if not asked:
        raise Fail("This ComfyUI has no ComfyUI-Manager restart route. Restart it the way you started it.")
    print("Restarting ComfyUI...", flush=True)
    for _ in range(60):
        try:
            comfy("/system_stats", timeout=2)
        except (Fail, ComfyError):
            break
        time.sleep(0.5)
    else:
        raise Fail("ComfyUI-Manager took the restart request, but ComfyUI didn't stop within 30 seconds. "
                   "Restart it by hand.")
    for _ in range(120):
        try:
            comfy("/system_stats", timeout=5)
            print("ComfyUI is back.")
            return
        except (Fail, ComfyError):
            time.sleep(2)
    raise Fail("ComfyUI didn't come back within 4 minutes. Check its log.")


# ---------------------------------------------------------------- add: workflow -> recipe

def is_api_format(wf):
    return isinstance(wf, dict) and "nodes" not in wf and bool(wf) and all(
        isinstance(v, dict) and "class_type" in v for v in wf.values())


def convert_with_comfy_cli(path):
    if not shutil.which("comfy"):
        raise Fail("To turn a saved workflow into a recipe I need comfy-cli (pip install comfy-cli), or an API "
                   "export: in ComfyUI open the workflow, then Workflow menu > Export (API), and run\n"
                   f"  python3 comfy_agent.py add {path} --api THE-EXPORT.json --name NAME")
    u = urllib.parse.urlparse(S["url"])
    r = subprocess.run(["comfy", "--json", "run", "--workflow", str(path), "--print-prompt", "--where", "local",
                        "--host", u.hostname or "127.0.0.1", "--port", str(u.port or 8188)],
                       capture_output=True, text=True, timeout=300)
    for line in r.stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        data = msg.get("data") or {}
        if isinstance(data.get("prompt"), dict):
            return data["prompt"]
    raise Fail("comfy-cli could not convert the workflow:\n" + (r.stderr or r.stdout)[-800:])


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "recipe"


def key_from(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def ui_nodes(ui):
    nodes = list(ui.get("nodes", []))
    for sg in (ui.get("definitions") or {}).get("subgraphs", []):
        nodes += sg.get("nodes", [])
    return nodes


def draft_card(prompt, ui, title):
    inputs = {}

    def add(key, spec):
        key = key or "setting"
        base, n = key, 2
        while key in inputs:
            key = f"{base}_{n}"
            n += 1
        inputs[key] = spec

    seeds, texts, media, sizes, primitives = [], [], [], {}, []
    for nid, node in prompt.items():
        ct = node["class_type"]
        label = (node.get("_meta") or {}).get("title") or ct
        if re.search(r"show|preview|note|save", ct, re.I):
            continue
        for field, val in node["inputs"].items():
            if isinstance(val, list):
                continue
            spec = input_spec(ct, field) or [None]
            typ = "COMBO" if isinstance(spec[0], list) else spec[0]
            opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            if field in ("seed", "noise_seed") and isinstance(val, int):
                seeds.append(f"{nid}.{field}")
            elif ct.startswith("Primitive") and field == "value" and label != ct and not isinstance(val, str):
                key = re.sub(r"^(int|float|boolean|bool|number)_", "", key_from(label))
                primitives.append((key, f"{nid}.{field}", val, label))
            elif typ == "STRING" and opts.get("multiline") and isinstance(val, str) and not val.lstrip().startswith(("[", "{")):
                texts.append((nid, field, label))
            elif typ == "COMBO" and not is_model_name(val) and (
                    opts.get("image_upload") or opts.get("video_upload") or opts.get("audio_upload")
                    or field in ("image", "video", "audio")):
                kind = ("video" if opts.get("video_upload") or field == "video" else
                        "audio" if opts.get("audio_upload") or field == "audio" else "image")
                media.append((nid, field, label, val, kind))
            elif field in ("width", "height", "length", "num_frames", "frames", "duration", "seconds") \
                    and isinstance(val, (int, float)) and field not in sizes:
                sizes[field] = (f"{nid}.{field}", val)
    for nid, field, label in texts:
        negative = "negative" in label.lower()
        named = key_from(label) if label != prompt[nid]["class_type"] else ""
        key = ("negative" if negative and "negative" not in inputs else
               "prompt" if not negative and "prompt" not in inputs else named or "text")
        add(key, {"set": f"{nid}.{field}", "default": prompt[nid]["inputs"][field], "help": label})
    for nid, field, label, val, kind in media:
        named = key_from(label)
        key = kind if kind not in inputs and named in ("", "load_image", "load_video", "load_audio") else named or kind
        add(key, {"set": f"{nid}.{field}", "type": kind, "default": val, "help": label})
    if seeds:
        add("seed", {"set": seeds, "default": "random", "help": "same seed and settings give the same result"})
    for field, (addr, val) in sizes.items():
        add(field, {"set": addr, "default": val, "help": field})
    for key, addr, val, label in primitives:
        add(key, {"set": addr, "default": val, "help": label})

    ui_by_type = {}
    for n in ui_nodes(ui) if ui else []:
        ui_by_type.setdefault(n.get("type"), n)
    packs = {}
    for ct in sorted({n["class_type"] for n in prompt.values()}):
        props = (ui_by_type.get(ct) or {}).get("properties") or {}
        module = (node_info(ct) or {}).get("python_module", "")
        if props.get("cnr_id") == "comfy-core" or (not props.get("cnr_id") and not module.startswith("custom_nodes.")):
            continue
        pack_id = props.get("cnr_id") or module.split(".", 1)[1]
        pack = packs.setdefault(pack_id, {"id": pack_id, "classes": []})
        if props.get("aux_id"):
            pack["repo"] = "https://github.com/" + props["aux_id"]
        elif "repo" not in pack and module.startswith("custom_nodes."):
            folder = Path(folder_paths()["custom_nodes"][0]) / module.split(".", 1)[1]
            r = subprocess.run(["git", "-C", str(folder), "remote", "get-url", "origin"], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                pack["repo"] = r.stdout.strip()
        pack["classes"].append(ct)

    meta = {}
    for n in ui_nodes(ui) if ui else []:
        for m in (n.get("properties") or {}).get("models") or []:
            if m.get("name"):
                meta[base_name(m["name"])] = m
    models, seen = [], set()
    for node in prompt.values():
        for val in node["inputs"].values():
            if not is_model_name(val) or base_name(val) in seen:
                continue
            file = base_name(val)
            seen.add(file)
            m = meta.get(file, {})
            models.append({"file": file, "folder": m.get("directory") or find_folder(file), "url": m.get("url")})
    return {"title": title, "does": "", "inputs": inputs, "tips": [],
            "needs": {"nodes": list(packs.values()), "models": models}}


def find_folder(file):
    try:
        folders = comfy("/models") or []
    except ComfyError:
        return None
    for folder in folders:
        if any(base_name(x) == file for x in models_in(folder)):
            return folder
    return None


def notes_text(ui):
    out = []
    for n in ui_nodes(ui) if ui else []:
        if n.get("type") in ("Note", "MarkdownNote") and n.get("widgets_values"):
            text = str(n["widgets_values"][0]).strip()
            if text:
                out.append(f"## {n.get('title') or n['type']}\n\n{text}")
    return "\n\n".join(out)


def cmd_add(a):
    src = Path(a.workflow).expanduser()
    wf = json.loads(src.read_text(encoding="utf-8"))
    name = slug(a.name or src.stem)
    folder = (Path(a.into).expanduser() if a.into else KIT / "recipes") / name
    if folder.exists() and not a.force:
        raise Fail(f"A recipe called {name} already exists ({folder}). Pick another --name, or add --force.")
    if is_api_format(wf):
        prompt, ui = wf, None
    else:
        ui = wf
        prompt = json.loads(Path(a.api).expanduser().read_text(encoding="utf-8")) if a.api else convert_with_comfy_cli(src)
    card = draft_card(prompt, ui, a.title or src.stem)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "prompt.json").write_text(json.dumps(prompt, indent=1), encoding="utf-8")
    (folder / "card.json").write_text(json.dumps(card, indent=1), encoding="utf-8")
    notes = notes_text(ui)
    if notes:
        (folder / "notes.md").write_text(notes + "\n", encoding="utf-8")
    if ui:
        shutil.copyfile(src, folder / "workflow.json")
    print(f"Made recipe {name} in {folder}")
    print(f"Settings found: {', '.join(card['inputs']) or 'none'}")
    print(f"Needs: {len(card['needs']['nodes'])} node pack(s), {len(card['needs']['models'])} model file(s)")
    print("Next: open card.json, give the settings clear names and help text, fill in title, does and tips,\n"
          f"remove settings nobody should change, then test: python3 comfy_agent.py run {name} --count 1")


# ---------------------------------------------------------------- MCP (stdio or HTTP)

MCP_TOOLS = [
    {"name": "recipes",
     "description": "List the ComfyUI recipes (ready workflows) you can run. Give a name to see that recipe's settings and tips.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"name": "generate",
     "description": "Run a recipe: queue count results, wait for them, return the files. Unset settings keep their "
                    "defaults. A list as a setting's value queues one job per item.",
     "inputSchema": {"type": "object", "required": ["recipe"], "properties": {
         "recipe": {"type": "string"},
         "settings": {"type": "object", "description": "setting name to value, e.g. {\"prompt\": \"...\"}"},
         "count": {"type": "integer", "minimum": 1, "maximum": 16},
         "send": {"type": "string", "description": "optional: 'phone' (or another target from config) to also send them there"}}}},
    {"name": "results",
     "description": "Collect jobs that were still running when generate returned. Each file is returned once.",
     "inputSchema": {"type": "object", "properties": {
         "operation": {"type": "string", "description": "optional: only this operation id"},
         "cancel": {"type": "boolean", "description": "cancel it (or every unfinished one) instead"}}}},
]


class _ThreadOut(io.TextIOBase):
    """sys.stdout for the MCP server: each tool call's prints go to its own buffer, so calls can run in
    parallel threads; everything else goes to the real stdout."""

    def __init__(self, real):
        self.real = real
        self.local = threading.local()

    def write(self, s):
        return (getattr(self.local, "buf", None) or self.real).write(s)

    def flush(self):
        (getattr(self.local, "buf", None) or self.real).flush()


@contextlib.contextmanager
def capture():
    buf = io.StringIO()
    if isinstance(sys.stdout, _ThreadOut):
        sys.stdout.local.buf = buf
        try:
            yield buf
        finally:
            sys.stdout.local.buf = None
    else:
        with contextlib.redirect_stdout(buf):
            yield buf


def mcp_wait(default):
    return int(os.environ.get("COMFY_AGENT_MCP_WAIT") or S.get("mcp_wait") or default)


def mcp_call(name, args, previews=True, stop=None, progress=None):
    done = []
    with capture() as buf:
        if name == "recipes":
            if args.get("name"):
                cmd_show(argparse.Namespace(recipe=args["name"], notes=False))
            else:
                cmd_list(None)
        elif name == "generate":
            r = get_recipe(args["recipe"])
            pairs = []
            for k, v in (args.get("settings") or {}).items():
                pairs += [(k, x) for x in v] if isinstance(v, list) else [(k, v)]
            count = max(1, min(16, int(args.get("count") or 1)))
            if managed():
                # Follow through the restore: a client whose model is unloaded can't read an early answer.
                reconcile()
                op_id = start_managed(r, pairs, count, send=args.get("send"))
                _, done = follow([op_id], mcp_wait(10800), stop=stop, progress=progress)
            else:
                jobs = queue_jobs(r, pairs, count)
                print(f"queued {len(jobs)} job(s) of {r.name}")
                left = wait_jobs(jobs, mcp_wait(240))
                done = [j for j in jobs if j not in left]
                files = [f for j in done for f in j.get("files") or []]
                if files and args.get("send"):
                    try:
                        print(send_files(files, args["send"]))
                    except Fail as e:
                        print(f"Sending failed (the files are saved): {e}")
        elif name == "results":
            done = mcp_results(args, stop, progress)
        else:
            raise Fail(f"unknown tool {name}")
    content = [{"type": "text", "text": buf.getvalue().strip()}]
    if previews:
        shown = 0
        for job in done:
            for it in job.get("remote") or []:
                if shown >= 4 or Path(it["filename"]).suffix.lower() not in IMAGE_EXT:
                    continue
                q = urllib.parse.urlencode({"filename": it["filename"], "subfolder": it.get("subfolder", ""),
                                            "type": it.get("type", "output"), "preview": "jpeg;70"})
                content.append({"type": "image", "mimeType": "image/jpeg",
                                "data": base64.b64encode(comfy("/view?" + q, raw=True)).decode()})
                shown += 1
    return content


def mcp_results(args, stop=None, progress=None):
    j = journal()
    if managed():
        reconcile()
    op = args.get("operation")
    if op and not j.op(op):
        raise Fail(f"No operation {op}.")
    if args.get("cancel"):
        cmd_cancel(argparse.Namespace(op=op))
    ids = [op] if op else undelivered_ops("managed")
    ids = [i for i in ids if j.op(i)["mode"] == "managed"]
    pending = [] if op else load_state().get("pending", [])
    old = [x for x in j.jobs(op, undelivered=True) if x["state"] in JOB_FINAL and x["op"] not in ids]
    if not ids and not pending and not old:
        print("No jobs are waiting.")
        return []
    done = report_jobs(j, old)
    wait = int(os.environ.get("COMFY_AGENT_MCP_RESULTS_WAIT", 60))
    if ids:
        _, got = follow(ids, wait if not managed() else mcp_wait(10800), stop=stop, progress=progress)
        done += got
    if pending:
        left = wait_jobs(pending, wait)
        done += [x for x in pending if x not in left]
    return [x for x in done if x["state"] == "done"]


def mcp_handle(msg, stop=None, notify=None):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "comfy-agent", "version": "1.1"}}
    elif method == "tools/list":
        result = {"tools": MCP_TOOLS}
    elif method == "tools/call":
        p = msg.get("params") or {}
        token = (p.get("_meta") or {}).get("progressToken")
        progress = None
        if token is not None and notify:
            def progress(n, text):
                notify({"jsonrpc": "2.0", "method": "notifications/progress",
                        "params": {"progressToken": token, "progress": n, "message": text}})
        try:
            result = {"content": mcp_call(p.get("name"), p.get("arguments") or {}, stop=stop, progress=progress)}
        except (Fail, ComfyError, AdapterError) as e:
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def mcp_stdio(stdin, real_out):
    """Answer requests in parallel threads, so a long generate never blocks results or a ping, and stop
    following an operation when the client sends notifications/cancelled. The operation itself goes on."""
    lock = threading.Lock()
    cancels = {}

    def send(obj):
        with lock:
            real_out.write(json.dumps(obj) + "\n")
            real_out.flush()

    def work(msg, ev):
        try:
            reply = mcp_handle(msg, stop=ev.is_set, notify=send)
        except Exception as e:  # never kill the server over one call
            reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                     "result": {"content": [{"type": "text", "text": f"{e.__class__.__name__}: {e}"}], "isError": True}}
        cancels.pop(msg.get("id"), None)
        if reply is not None and not ev.is_set():
            send(reply)

    threads = []
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("method") == "notifications/cancelled":
            ev = cancels.get((msg.get("params") or {}).get("requestId"))
            if ev:
                ev.set()
            continue
        if msg.get("method") == "tools/call" and msg.get("id") is not None:
            ev = cancels[msg["id"]] = threading.Event()
            t = threading.Thread(target=work, args=(msg, ev), daemon=True)
            t.start()
            threads.append(t)
            continue
        reply = mcp_handle(msg)
        if reply is not None:
            send(reply)
    for t in threads:
        t.join(timeout=1)


def cmd_mcp(a):
    real = sys.stdout
    sys.stdout = _ThreadOut(real)
    with contextlib.suppress(Exception):
        if managed():
            reconcile()
    if not a.http:
        mcp_stdio(sys.stdin, real)
        return
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    token = a.token or os.environ.get("COMFY_AGENT_TOKEN")
    if not token or len(token) < 16:
        raise Fail("The HTTP server needs a long secret: --token (16+ characters) or COMFY_AGENT_TOKEN.")

    class Handler(BaseHTTPRequestHandler):
        def allowed(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            return path == f"/mcp/{token}" or (path == "/mcp" and self.headers.get("Authorization") == f"Bearer {token}")

        def do_POST(self):
            if not self.allowed():
                self.send_error(404)
                return
            msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            reply = mcp_handle(msg)
            if reply is None:
                self.send_response(202)
                self.end_headers()
                return
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.send_error(405 if self.allowed() else 404)

        def log_message(self, *args):
            pass

    print(f"MCP server on http://127.0.0.1:{a.http}/mcp/<token> (this computer only). "
          "To reach it from a chat app, see docs/chat-apps.md", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.http), Handler).serve_forever()


# ---------------------------------------------------------------- main

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(prog="comfy_agent.py", description="Make pictures and videos with ComfyUI recipes.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the recipes")
    p = sub.add_parser("show", help="a recipe's settings")
    p.add_argument("recipe")
    p.add_argument("--notes", action="store_true", help="also print the workflow author's notes")
    for name in ("run", "wait"):
        p = sub.add_parser(name, help="queue a recipe and save results" if name == "run" else "collect queued jobs")
        if name == "run":
            p.add_argument("recipe")
            p.add_argument("values", nargs="*", help="setting=value; repeat a setting to queue one job per value")
            p.add_argument("--count", "-n", type=int, default=1, help="results per setting combination")
            p.add_argument("--no-wait", action="store_true", help="queue and return at once")
            p.add_argument("--set", action="append", metavar="NODE.input=VALUE", help="set any node input directly")
        p.add_argument("--send", nargs="?", const="phone", help="also send the results (default target: phone)")
        p.add_argument("--free", action="store_true", help="unload ComfyUI's models afterwards (frees the GPU)")
        p.add_argument("--timeout", type=int, default=600, help="seconds to wait before giving the prompt back")
    p = sub.add_parser("send", help="send files to a phone")
    p.add_argument("files", nargs="*", help="files, or 'last' (the default)")
    p.add_argument("--to", default="phone")
    sub.add_parser("doctor", help="check ComfyUI and the recipes")
    p = sub.add_parser("install", help="install what a recipe needs")
    p.add_argument("recipe")
    p.add_argument("--yes", action="store_true", help="do it (without this, only show the plan)")
    sub.add_parser("restart", help="restart ComfyUI through ComfyUI-Manager")
    p = sub.add_parser("add", help="make a recipe from a workflow file")
    p.add_argument("workflow")
    p.add_argument("--name")
    p.add_argument("--title")
    p.add_argument("--api", help="the workflow's Export (API) file, if comfy-cli can't convert it")
    p.add_argument("--into", help="folder to put the recipe in (default: recipes/)")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("gallery", help="share the results folder on your tailnet")
    p.add_argument("--off", action="store_true")
    p.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    p = sub.add_parser("config", help="show or change settings")
    p.add_argument("values", nargs="*", help="url=..., out=..., unload_llm=COMMAND, phone=tailscale:DEVICE or phone=ntfy:TOPIC")
    p = sub.add_parser("ops", help="recent operations, their state and timings")
    p.add_argument("op", nargs="?")
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("cancel", help="cancel an operation (default: every unfinished one)")
    p.add_argument("op", nargs="?")
    p = sub.add_parser("reconcile", help="finish operations whose runner died, retry blocked ones")
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--timeout", type=int, default=600)
    p = sub.add_parser("_runner", help=argparse.SUPPRESS)
    p.add_argument("op")
    p.add_argument("--out", required=True)
    p = sub.add_parser("mcp", help="run as an MCP server")
    p.add_argument("--http", type=int, help="serve Streamable HTTP on this port instead of stdio")
    p.add_argument("--token", help="secret for the HTTP server")
    a = ap.parse_args(argv)
    try:
        globals()["cmd_" + a.cmd](a)
    except Fail as e:
        if str(e):
            print(str(e), file=sys.stderr)
        return 1
    except ComfyError as e:
        print("ComfyUI said: " + explain(e.detail), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
