#!/usr/bin/env python3
"""comfy-agent: let any AI agent make pictures and videos with ComfyUI.

Python 3.9 or newer, standard library only. It talks to ComfyUI over its HTTP API.

  comfy_agent.py list                         the recipes, one line each
  comfy_agent.py show RECIPE [--notes]        a recipe's settings, defaults and tips
  comfy_agent.py run RECIPE [name=value ...]  queue it, wait, save the results
        [--count N] [--send [TARGET]] [--no-wait] [--free] [--set NODE.input=VALUE]
  comfy_agent.py wait [--send [TARGET]]       wait for queued jobs and save their results
  comfy_agent.py send [FILE ... | last] [--to TARGET]
  comfy_agent.py redo FILE [name=value ...]   make a result again: same recipe and settings, a new seed
  comfy_agent.py doctor                       is ComfyUI up, which recipes are ready
  comfy_agent.py install RECIPE [--yes]       install a recipe's node packs and models
  comfy_agent.py restart                      restart ComfyUI (after new node packs)
  comfy_agent.py add WORKFLOW.json --name NAME  turn a ComfyUI workflow into a recipe
  comfy_agent.py config [key=value ...]       url, out, and where "phone" is
  comfy_agent.py stats [--share]              your own usage totals, if you turned stats on
  comfy_agent.py mcp [--http PORT --token T]  the same thing as an MCP server with 3 tools
"""
from __future__ import annotations

import argparse
import base64
import collections
import contextlib
import copy
import datetime as dt
import difflib
import hashlib
import http.client
import io
import ipaddress
import json
import math
import mimetypes
import os
import platform
import random
import re
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

VERSION = "1.0"
KIT = Path(__file__).resolve().parent
CONFIG = KIT / "comfy-agent.local.json"
MODEL_EXT = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".sft", ".onnx")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv")
UPLOAD_EXT = {"image": IMAGE_EXT + (".bmp", ".tif", ".tiff", ".avif"), "video": VIDEO_EXT + (".avi", ".m4v"),
              "audio": (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".opus")}
MEDIA_KINDS = (*UPLOAD_EXT, "file")


class Fail(Exception):
    """A problem to explain to the person. Printed without a traceback.

    kind is one of STATS_ERRORS, for the optional usage log; the message itself is never logged."""

    def __init__(self, *args, kind="other"):
        super().__init__(*args)
        self.kind = kind


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
    return s


S = load_settings()


def cli_command():
    return "python comfy_agent.py" if os.name == "nt" else "python3 comfy_agent.py"


def out_dir():
    return Path(S["out"]).expanduser()


# ---------------------------------------------------------------- HTTP

class CredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        before, after = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
            if (after.scheme == before.scheme == "https" and after.hostname == before.hostname
                    and after.port in (None, 443) and after.username is None and req.has_header("Authorization")):
                redirected.add_unredirected_header("Authorization", req.get_header("Authorization"))
        return redirected


def open_url(url, method="GET", body=None, headers=None, timeout=60):
    headers = dict(headers or {})
    data = body
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    if req.has_header("Authorization"):
        return urllib.request.build_opener(CredentialRedirect()).open(req, timeout=timeout)
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
                   f"Start ComfyUI first. If it runs on another address: {cli_command()} config url=http://HOST:PORT",
                   kind="ComfyUI not answering") from None
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
            total = int(total) if total else None
            if r.status == 206:
                span = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", r.headers.get("Content-Range", ""))
                if (not span or int(span[1]) != have or not have <= int(span[2]) < int(span[3])
                        or (total is not None and total != int(span[2]) - have + 1)):
                    raise Fail("The server returned the wrong part of the file. Try the download again.")
                total = int(span[3])
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
            if not done or (total is not None and done != total):
                raise Fail("The download ended before the whole file arrived. Try again to resume.")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise Fail(f"{url} needs a login (HTTP {e.code}). Set HF_TOKEN (Hugging Face) or CIVITAI_API_KEY (Civitai) "
                       "and try again. Some models also need their license accepted on the website first.") from None
        raise Fail(f"Download failed, HTTP {e.code}: {url}") from None
    except (http.client.HTTPException, OSError) as e:
        raise Fail(f"Download stopped ({e}). Run the same command again to resume.") from None
    part.replace(dest)
    return dest


def env_first(*names):
    return next((os.environ[n] for n in names if os.environ.get(n)), None)


def auth_headers(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username is not None or parsed.port not in (None, 443):
        return {}
    host = parsed.hostname
    key = None
    if host == "huggingface.co":
        key = env_first("HF_TOKEN", "HUGGINGFACE_TOKEN", "HF_API_TOKEN")
    elif host == "civitai.com":
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


def get_recipe(name, *, registered=False):
    if not isinstance(name, str):
        raise Fail("Choose a recipe by name.", kind="bad setting")
    if not registered:
        p = Path(name).expanduser()
        if (p / "card.json").exists():
            return stats_recipe(Recipe(p))
    found = all_recipes()
    if name in found:
        return stats_recipe(Recipe(found[name]))
    near = difflib.get_close_matches(name, list(found), n=3, cutoff=0.4)
    hint = f" Did you mean {' or '.join(near)}?" if near else ""
    raise Fail(f"No recipe called '{name}'.{hint} See them all: {cli_command()} list", kind="bad setting")


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
    if not key:
        raise Fail(f"'{addr}' is not NODE.input. --set takes a node input such as 23.seed=5; "
                   f"a recipe setting such as {addr} is given as {addr}=VALUE without --set.", kind="bad setting")
    node = prompt.get(nid)
    if node is None:
        raise Fail(f"The recipe points at node {nid}, which is not in its prompt.json.", kind="bad setting")
    if key not in node["inputs"]:
        raise Fail(f"Node {nid} ({node['class_type']}) has no input '{key}'. It has: {', '.join(node['inputs'])}",
                   kind="bad setting")
    if isinstance(node["inputs"][key], list):
        raise Fail(f"{addr} is wired to another node, so it can't be set to a value.", kind="bad setting")
    node["inputs"][key] = value


def coerce(value, kind, key="setting"):
    if kind in ("number", "seed"):
        if isinstance(value, str):
            try:
                value = int(value)
            except ValueError:
                if kind == "number":
                    with contextlib.suppress(ValueError):
                        value = float(value)
        if type(value) is int or (kind == "number" and type(value) is float and math.isfinite(value)):
            return value
        raise Fail(f"{key} needs a whole number or random." if kind == "seed" else f"{key} needs a number.",
                   kind="bad setting")
    if kind == "bool":
        word = str(value).lower()
        if word in ("1", "true", "yes", "on"):
            return True
        if word in ("0", "false", "no", "off"):
            return False
        raise Fail(f"Use true or false for {key}.", kind="bad setting")
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
            raise Fail(f"'{item}' should look like name=value.", kind="bad setting")
        k, v = item.split("=", 1)
        pairs.append((k.strip(), v))
    return pairs


def plan_jobs(recipe, pairs, count):
    """One dict of settings per job. A setting given several times queues one job per value
    (several such settings are paired in order). count repeats each."""
    given = {}
    for k, v in pairs:
        if k not in recipe.inputs:
            raise Fail(f"{recipe.name} has no setting '{k}'. Its settings: {', '.join(recipe.inputs)}", kind="bad setting")
        given.setdefault(k, []).append(v)
    lengths = {len(v) for v in given.values() if len(v) > 1}
    if len(lengths) > 1:
        raise Fail("Settings given more than once are paired in order, so give each one the same number of times.",
                   kind="bad setting")
    rounds = lengths.pop() if lengths else 1
    for key, spec in recipe.inputs.items():
        if spec.get("required") and any(v is None or isinstance(v, str) and not v.strip()
                                        for v in given.get(key, [None])):
            raise Fail(f"{recipe.name} needs {key}=... ({spec.get('help') or kind_of(key, spec)}).", kind="bad setting")
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

    def locate(fields, key, opts):
        val = fields[key]
        if opts is None or val in opts:
            return False
        for option in opts:
            if isinstance(option, str) and base_name(option) == base_name(val):
                fields[key] = option
                return True
        missing_models.append(base_name(val))
        return False

    for node in prompt.values():
        if node_info(node["class_type"]) is None:
            missing_nodes.append(node["class_type"])
        inputs = node["inputs"]
        for key, val in inputs.items():
            if is_model_name(val):
                locate(inputs, key, combo_options(node["class_type"], key))
        value = inputs.get("loras")
        if node["class_type"] == "AUSBOSS_NODES_LoraLoader" and isinstance(value, str) and value.strip():
            try:
                rows = json.loads(value)
            except ValueError:
                rows = None
            if not isinstance(rows, list):
                raise Fail("The LoRA loader needs a list of model files.")
            changed = False
            for row in rows:
                if (isinstance(row, dict) and row.get("enabled", True) and not row.get("group_off")
                        and is_model_name(row.get("name"))):
                    changed = locate(row, "name", models_in("loras")) or changed
            if changed:
                inputs["loras"] = json.dumps(rows, ensure_ascii=False)
    return sorted(set(missing_nodes)), sorted(set(missing_models))


def not_ready_message(recipe, nodes, models):
    parts = []
    if nodes:
        parts.append(f"node types {', '.join(nodes)}")
    if models:
        parts.append(f"model files {', '.join(models)}")
    return (f"{recipe.name} can't run yet: this ComfyUI is missing {' and '.join(parts)}.\n"
            f"See what would be installed: {cli_command()} install {recipe.name}")


# ---------------------------------------------------------------- run

def upload(path, cache):
    p = Path(path).expanduser()
    if p in cache:
        return cache[p]
    if not p.is_file():
        raise Fail(f"Can't find the file {p}", kind="bad setting")
    with p.open("rb") as stream:
        digest = hashlib.sha1()
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
        size = stream.tell()
        stream.seek(0)
        name = digest.hexdigest()[:8] + "-" + re.sub(r"[^\w.-]+", "_", p.name)
        boundary = uuid.uuid4().hex
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode()
        tail = f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n--{boundary}--\r\n'.encode()

        def body():
            yield head
            while stream.tell() < size:
                chunk = stream.read(min(size - stream.tell(), 1 << 20))
                if not chunk:
                    raise Fail("The input file changed during the upload.")
                yield chunk
            yield tail

        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(head) + size + len(tail))}
        res = comfy("/upload/image", body(), headers=headers, timeout=300)
    cache[p] = f"{res['subfolder']}/{res['name']}" if res.get("subfolder") else res["name"]
    return cache[p]


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
    if status.get("completed") is False or status.get("status_str") in ("error", "failed", "interrupted", "cancelled"):
        return "ComfyUI stopped before the job finished."


def state_file():
    return out_dir() / ".comfy-agent-jobs.json"


@contextlib.contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_state():
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"pending": [], "last": []}
    except (OSError, ValueError) as e:
        raise Fail(f"Cannot read job records at {state_file()}: {e}") from None


def save_state(state):
    write_json(state_file(), state)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def queue_jobs(recipe, pairs, count=1, raw_sets=(), extra=None, *, transport="cli", by=None, request=None, review=None):
    from generation_receipt import graph_receipt
    if any(value is not None and not isinstance(value, str) for value in (by, request)):
        raise Fail("by and request must be text when supplied.", kind="bad setting")
    if by is not None:
        if (not re.fullmatch(r"[\w .()+'-]{1,80}", by) or not by.strip()
                or by.strip().lower().startswith(("www.", "bearer ", "basic ", "authorization "))
                or re.fullmatch(r"\d+(?:\.\d+){3}", by.strip())):
            raise Fail("Use a short display name for by, not an address, URL or credential.", kind="bad setting")
        by = by.strip()
    jobs_settings = plan_jobs(recipe, pairs, count)
    base = recipe.prompt()
    nodes, models = preflight(base)
    if nodes or models:
        raise Fail(not_ready_message(recipe, nodes, models), kind="node missing" if nodes else "model missing")
    unload_llm()
    devices = []
    with contextlib.suppress(Fail, ComfyError, ValueError):
        stats = comfy("/system_stats", timeout=3)
        listed = stats.get("devices") if isinstance(stats, dict) else None
        if isinstance(listed, list):
            devices = [d["name"] for d in listed if isinstance(d, dict) and isinstance(d.get("name"), str)
                       and d["name"] and d.get("type") != "cpu" and d["name"].lower() != "cpu"]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:12]
    uploads, jobs = {}, []
    for i, given in enumerate(jobs_settings):
        prompt = copy.deepcopy(base)
        job = {"recipe": recipe.name, "dir": str(recipe.folder.resolve()), "n": i + 1, "of": len(jobs_settings),
               "stamp": stamp, "run": f"{recipe.name}/{stamp}", "settings": {}, "given": {}, **(extra or {})}
        if raw_sets:
            job["sets"] = [list(s) for s in raw_sets]
        for key, spec in recipe.inputs.items():
            kind = kind_of(key, spec)
            value = given.get(key, spec.get("default"))
            if key in given:
                if kind in MEDIA_KINDS:
                    if not isinstance(value, str):
                        raise Fail(f"Give a file path for {key}.", kind="bad setting")
                    value = Path(value).expanduser().resolve()
                    allowed = UPLOAD_EXT[kind] if kind != "file" else sum(UPLOAD_EXT.values(), ())
                    if value.suffix.lower() not in allowed:
                        raise Fail(f"Choose a media file for {key} (expected {kind}).", kind="bad setting")
                job["given"][key] = str(value) if kind in MEDIA_KINDS else value
            if kind == "seed":
                value = random.randint(1, 2 ** 48) if value in (None, "", "random") else coerce(value, kind, key) + i
                job["seed"] = value
            elif (value is None or value == "") and not (key in given and kind in ("bool", "number")):
                continue
            elif kind in MEDIA_KINDS:
                if key not in given:
                    continue
                value = upload(value, uploads)
            else:
                value = coerce(value, kind, key)
            for addr in targets(spec):
                put(prompt, addr, value)
            if key in given or kind == "seed":
                job["settings"][key] = value if not isinstance(value, str) or len(value) <= 120 else value[:117] + "..."
        for addr, value in raw_sets:
            put(prompt, addr, json_or_text(value))
        receipt = graph_receipt(recipe.folder, recipe.card, prompt, given)
        receipt.update(source={"transport": transport, "by": by}, request=request, review=review,
                       execution={"prompt_id": None, "submitted_at": now_iso(), "queued_at": None, "started_at": None,
                                  "completed_at": None, "queue_seconds": None, "run_seconds": None, "devices": devices})
        job["receipt"] = receipt
        submit_job(job, prompt)
        jobs.append(job)
    return jobs


def submit_job(job, prompt):
    # Save a unique client ID before POST. A lost reply can be matched in the queue or history.
    job.update(id=str(uuid.uuid4()), unconfirmed=True)
    with file_lock(state_file().with_suffix(".lock")):
        state = load_state()
        state.setdefault("pending", []).append(job)
        save_state(state)
        try:
            reply = comfy("/prompt", {"prompt": prompt, "client_id": job["id"]})
        except ComfyError as e:
            if 400 <= e.status < 500:
                state["pending"].pop()
                save_state(state)
            raise Fail("ComfyUI refused the job:\n" + explain(e.detail), kind=refusal_kind(e.detail)) from None
        if not isinstance(reply, dict) or not reply.get("prompt_id"):
            raise Fail("ComfyUI did not confirm the job. Run wait to check it.")
        job["receipt"]["execution"]["prompt_id"] = job["id"] = reply["prompt_id"]
        del job["unconfirmed"]
        if reply.get("node_errors"):
            job["error"] = "ComfyUI skipped part of the workflow. " + explain(reply)
        save_state(state)
        if job.get("error"):
            print(job["error"], flush=True)


def recover_submissions(jobs):
    unknown = {j["id"]: j for j in jobs if j.get("unconfirmed")}
    if not unknown:
        return
    q = comfy("/queue")

    def match(rows):
        for row in rows:
            if isinstance(row, list) and len(row) > 3 and isinstance(row[3], dict):
                client = row[3].get("client_id")
                if client in unknown:
                    j = unknown.pop(client)
                    j["receipt"]["execution"]["prompt_id"] = j["id"] = row[1]
                    j["client_id"] = client
                    del j["unconfirmed"]

    match(q["queue_running"] + q["queue_pending"])
    if unknown:
        match(entry.get("prompt") for entry in comfy("/history").values())


def save_outputs(entry, job):
    items, seen = [], set()
    for node_out in (entry.get("outputs") or {}).values():
        for val in node_out.values():
            for it in val if isinstance(val, list) else []:
                if isinstance(it, dict) and it.get("filename") and it.get("type") != "input":
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
        name = f"{job['stamp']}-{job['n']:02d}" + (f"-{i}" if len(keep) > 1 else "")
        suffix, k = Path(it["filename"]).suffix.lower(), 2
        dest = folder / f"{name}{suffix}"
        while dest.exists():
            dest, k = folder / f"{name}-{k}{suffix}", k + 1
        download(S["url"] + "/view?" + q, dest)
        saved.append(str(dest))
    job["remote"] = keep
    return saved


def history_times(entry):
    def moment(value):
        if type(value) in (float, int):
            with contextlib.suppress(ValueError, OSError, OverflowError):
                return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc)
        return None

    prompt = entry.get("prompt")
    queued = (moment(prompt[3].get("create_time")) if isinstance(prompt, (tuple, list)) and len(prompt) > 3
              and isinstance(prompt[3], dict) else None)
    events = {}
    for item in (entry.get("status") or {}).get("messages") or []:
        if (isinstance(item, (tuple, list)) and len(item) == 2 and item[0] in ("execution_start", "execution_success")
                and isinstance(item[1], dict)):
            events[item[0]] = moment(item[1].get("timestamp"))
    started, completed = events.get("execution_start"), events.get("execution_success")
    return {"queued_at": queued.isoformat() if queued else None, "started_at": started.isoformat() if started else None,
            "completed_at": completed.isoformat() if completed else None,
            "queue_seconds": (started - queued).total_seconds() if started and queued and started >= queued else None,
            "run_seconds": (completed - started).total_seconds() if completed and started and completed >= started else None}


def wait_jobs(jobs, timeout=600):
    """Wait for jobs, saving each one's files as it finishes. Returns the jobs still running."""
    start, last_check = time.time(), time.time()
    left = list(jobs)
    recover_submissions(left)
    while left:
        for job in list(left):
            entry = (comfy(f"/history/{job['id']}") or {}).get(job["id"])
            if not entry:
                continue
            left.remove(job)
            status = entry.get("status") or {}
            job["files"] = save_outputs(entry, job)
            error = run_error(status) or job.get("error")
            if error or not job["files"]:
                job["error"] = error or "ComfyUI saved no files."
                print(f"FAILED {job['n']}/{job['of']}: {job['error']}", flush=True)
                if not job["files"]:
                    continue
            times = history_times(entry)
            if "receipt" in job:
                job["receipt"]["execution"].update(times)
            took = f", {times['run_seconds']:.1f} s" if times["run_seconds"] is not None else ""
            seed = f", seed {job['seed']}" if "seed" in job else ""
            label = "saved" if job.get("error") else "done"
            print(f"{label} {job['n']}/{job['of']}{seed}{took}, run {job.get('run') or job['recipe'] + '/' + job['stamp']}: "
                  f"{', '.join(job['files'])}", flush=True)
        if not left:
            break
        if time.time() - start > timeout:
            print(f"{len(left)} job(s) still running. Collect them later with: {cli_command()} wait", flush=True)
            break
        if time.time() - last_check > 15:
            last_check = time.time()
            recover_submissions(left)
            q = comfy("/queue") or {}
            live = {item[1] for item in q.get("queue_running", []) + q.get("queue_pending", [])}
            for job in [j for j in left if j["id"] not in live]:
                if not (comfy(f"/history/{job['id']}") or {}).get(job["id"]):
                    left.remove(job)
                    job["error"] = "ComfyUI lost this job (was it restarted?)"
                    print(f"FAILED {job['n']}/{job['of']}: {job['error']}", flush=True)
        time.sleep(1)
    finished = [j for j in jobs if j not in left]
    finished_ids = {key for j in finished for key in (j["id"], j.get("client_id"))}
    files = [f for j in finished for f in j.get("files", [])]
    with file_lock(state_file().with_suffix(".lock")):
        state = load_state()
        recovered = {j["client_id"]: j for j in left if j.get("client_id")}
        state["pending"] = [recovered.get(j["id"], j) for j in state.get("pending", []) if j["id"] not in finished_ids]
        if files:
            state["last"] = files
            made = state.setdefault("made", {})
            for j in finished:
                for f in j.get("files", []):
                    made[Path(f).relative_to(out_dir()).as_posix()] = {
                        k: j[k] for k in ("recipe", "dir", "run", "given", "sets", "seed", "redo_of", "note", "receipt") if k in j}
        save_state(state)
    return left


def free_vram():
    comfy("/free", {"unload_models": True, "free_memory": True})
    print("Asked ComfyUI to unload its models and free the GPU.")


def unload_llm():
    """Small-card mode: take the agent's own model off the GPU while ComfyUI works.

    The model server loads it back by itself on the agent's next request (llama-swap, LM Studio, Ollama)."""
    cmd = S.get("unload_llm")
    if not cmd:
        return
    try:
        r = subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    except subprocess.TimeoutExpired:
        raise Fail("Unloading the language model took over 2 minutes.") from None
    except (OSError, ValueError):
        raise Fail("Could not start the command to unload the language model.") from None
    if r.returncode:
        raise Fail(f"Unloading the language model failed (exit {r.returncode}). Check your unload_llm setting.")
    print("Unloaded the language model so ComfyUI has the whole card. It loads back on your next turn.", flush=True)


def cmd_run(a):
    if a.send:
        send_target(a.send)
    recipe = get_recipe(a.recipe)
    jobs = queue_jobs(recipe, parse_pairs(a.values), max(1, a.count), parse_pairs(a.set or []),
                      by=getattr(a, "by", None), request=getattr(a, "request", None))
    print(f"queued {len(jobs)} job(s) of {recipe.name}; run {jobs[0]['run']}", flush=True)
    collect(jobs, a)


def cmd_redo(a):
    if a.send:
        send_target(a.send)
    collect(queue_redo(a.file, parse_pairs(a.values), max(1, a.count), variation=getattr(a, "variation", False),
                       by=getattr(a, "by", None), request=getattr(a, "request", None)), a)


def collect(jobs, a):
    if a.no_wait and S.get("unload_llm"):
        print("Waiting anyway: the language model is unloaded, and it must not load back until these are done.")
    elif a.no_wait:
        print(f"Collect the results with: {cli_command()} wait")
        check_jobs(jobs)
        return
    left = wait_jobs(jobs, a.timeout)
    finish(jobs, left, a)


def cmd_wait(a):
    with file_lock(state_file().with_suffix(".lock")):
        pending = load_state().get("pending", [])
    if not pending:
        print("No jobs are waiting.")
        return
    print(f"waiting for {len(pending)} job(s)", flush=True)
    left = wait_jobs(pending, a.timeout)
    finish(pending, left, a)


def check_jobs(jobs, message="Some jobs failed. See the job messages above."):
    if any(j.get("error") for j in jobs):
        raise Fail(message)


def finish(jobs, left, a):
    files = [f for j in jobs if j not in left for f in j.get("files", [])]
    if files and a.send:
        print(send_files(files, a.send))
    if (a.free or S.get("unload_llm")) and not left:
        free_vram()
    check_jobs(jobs)


# ---------------------------------------------------------------- send

def send_target(target):
    dest = S.get("send", {}).get(target, target)
    if isinstance(dest, str) and dest.startswith(("tailscale:", "ntfy:")) and dest.partition(":")[2].strip():
        return dest
    raise Fail(f"Don't know where '{target}' is. Set it once: {cli_command()} config phone=tailscale:DEVICE "
               "(or phone=ntfy:TOPIC). Your Tailscale devices: tailscale status")


def send_files(files, target):
    dest = send_target(target)
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
                try:
                    open_url(url, "PUT", fh.read(), {"Filename": Path(f).name, "Title": "ComfyUI: " + Path(f).parent.name},
                             timeout=300).close()
                except (OSError, ValueError, http.client.HTTPException):
                    raise Fail("Could not send the file with ntfy. Check the connection and topic settings.") from None
        return f"Sent {len(files)} file(s) with ntfy."


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


# ---------------------------------------------------------------- redo

def result_run(rel, made):
    if made.get("run"):
        return made["run"]
    p = Path(rel)
    stamp = re.fullmatch(r"(\d{8}-\d{6}(?:-[0-9a-f]{12})?)-\d+(?:-\d+)*", p.stem)
    return (p.parent / stamp[1]).as_posix() if stamp else "file:" + rel


def load_marks():
    path = out_dir() / ".comfy-agent-marks.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"files": {}}
    except (OSError, ValueError) as e:
        raise Fail(f"Cannot read review records at {path}: {e}") from None


def redo_answered(rel):
    """True once a redo of REL that answers its review request is queued or made: one request, one answer."""
    state = load_state()
    for job in [*state.get("made", {}).values(), *state.get("pending", [])]:
        origin = (job.get("receipt") or {}).get("review") or {}
        if origin.get("kind") == "redo" and origin.get("file") == rel and origin.get("requested_redo") is True:
            return True
    return False


def rel_out(file):
    """FILE (absolute, or relative to here or to the outputs folder) as a path inside the outputs folder."""
    root = out_dir().resolve()
    p = Path(file).expanduser()
    for c in (p,) if p.is_absolute() else (Path.cwd() / p, root / p):
        c = c.resolve()
        if c.is_file() and root in c.parents:
            return c.relative_to(root).as_posix()
    raise Fail(f"Can't find {file} in {root}")


def queue_redo(file, overrides, count=1, *, variation=False, transport="cli", by=None, request=None):
    """Queue the recipe that made FILE again, with its settings and a new seed. overrides change settings."""
    rel = rel_out(file)
    made = load_state().get("made", {}).get(rel)
    if not made:
        raise Fail(f"There's no record of how {rel} was made (the kit keeps one for results it made), so make it "
                   "again with run instead.")
    folder = made.get("dir", "")
    recipe = get_recipe(folder if folder and (Path(folder) / "card.json").exists() else made["recipe"])
    changed = {k for k, _ in overrides}
    keep = [(k, v) for k, v in (made.get("given") or {}).items()
            if k in recipe.inputs and k not in changed and kind_of(k, recipe.inputs[k]) != "seed"]
    marks = load_marks()
    run = result_run(rel, made)
    review = marks.get("runs", {}).get(run, {})
    finished = review.get("done_at") if rel in review.get("files", []) else None
    entry = marks.get("files", {}).get(rel, {}) if finished else {}
    if variation and entry.get("mark") != "good":
        raise Fail("A variation needs a Good result from a finished review.")
    asked = not variation and entry.get("redo") is True and not redo_answered(rel)
    origin = {"kind": "variation" if variation else "redo", "run": run, "file": rel, "finished_at": finished,
              "mark": entry.get("mark", "") if finished else None, "note": entry.get("note", "") if finished else None,
              "requested_redo": asked}
    extra = {} if variation else {"redo_of": rel}
    if entry.get("note"):
        extra["note"] = entry["note"]
    replaced = set()
    for key, spec in recipe.inputs.items():
        if kind_of(key, spec) == "seed":
            spec["default"] = "random"
            replaced.update(targets(spec))
        elif key in changed:
            replaced.update(targets(spec))
    sets = [(addr, value) for addr, value in made.get("sets", []) if addr not in replaced]
    jobs = queue_jobs(recipe, keep + list(overrides), count, sets, extra,
                      transport=transport, by=by, request=request, review=origin)
    note = f' (its note: {json.dumps(entry["note"])})' if entry.get("note") else ""
    print(f"queued {len(jobs)} {origin['kind']}(s) of {rel} with {recipe.name}{note}; run {jobs[0]['run']}", flush=True)
    return jobs


# ---------------------------------------------------------------- list, show, doctor, config

def cmd_list(a):
    found = all_recipes()
    if not found:
        print(f"No recipes yet. Make one from a workflow: {cli_command()} add WORKFLOW.json --name NAME")
        return
    for name, folder in found.items():
        r = Recipe(folder)
        settings = ", ".join(k + ("*" if v.get("required") else "") for k, v in r.inputs.items())
        print(f"{name}: {r.card.get('title', '')} [{settings}]")
    print(f"* = required. Details: {cli_command()} show RECIPE")


def cmd_show(a, *, registered=False):
    r = get_recipe(a.recipe, registered=registered)
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
        print(f"The workflow author's notes: {cli_command()} show", r.name, "--notes")


def cmd_doctor(a):
    st = comfy("/system_stats", timeout=10)
    print(f"ComfyUI {st['system'].get('comfyui_version', '?')} is up at {S['url']}.")
    for dev in st.get("devices", [])[:2]:
        name = re.sub(r"^cuda:\d+\s+", "", dev["name"]).partition(" : ")[0]
        print(f"GPU: {name}, {gb(dev.get('vram_free'))} free of {gb(dev.get('vram_total'))}.")
    q = comfy("/queue")
    print(f"Queue: {len(q['queue_running'])} running, {len(q['queue_pending'])} waiting.")
    for name, folder in all_recipes().items():
        r = Recipe(folder)
        nodes, models = preflight(r.prompt())
        print(f"  {name}: " + ("ready" if not (nodes or models) else
                               f"not ready ({len(nodes)} node types, {len(models)} models missing). "
                               f"{cli_command()} install {name}"))
    targets_ = [f"{k} = {'ntfy' if v.startswith('ntfy:') else v}" for k, v in S.get("send", {}).items()]
    print("Send to: " + (", ".join(targets_) or
                         f"nothing set up ({cli_command()} config phone=tailscale:DEVICE)"))
    pending = load_state().get("pending", [])
    if pending:
        print(f"{len(pending)} queued job(s) not collected yet: {cli_command()} wait")
    if stats_enabled():
        print(f"Usage stats: on, in stats.jsonl in your outputs folder; never sent anywhere. See them: {cli_command()} stats")


RETIRED_SETTINGS = ("gallery_path", "gallery_port", "gallery_listen", "gallery_url")


def cmd_config(a):
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    for k, v in parse_pairs(a.values):
        if k == "stats":
            word = {"on": "on", "yes": "on", "true": "on", "off": "off", "no": "off", "false": "off"}.get(v.strip().lower())
            if not word:
                raise Fail("stats is on or off: config stats=on", kind="bad setting")
            cfg[k] = word
        elif k in ("url", "out", "unload_llm", "install"):
            cfg[k] = v
        elif k in RETIRED_SETTINGS:
            raise Fail(f"{k} belongs to the review page, which is now the comfy-agent-review add-on: "
                       f"python3 review.py config {k}=... in its folder.", kind="bad setting")
        else:
            cfg.setdefault("send", {})[k] = v
    if a.values:
        CONFIG.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    keys = {k for k, _ in parse_pairs(a.values)}
    if keys != {"stats"}:  # turning stats on or off doesn't print the other settings
        print(json.dumps({"url": S["url"], "out": S["out"], **{k: v for k, v in cfg.items() if k not in RETIRED_SETTINGS}},
                         indent=1))
    if "stats" in keys:
        print("Usage stats are on: each command adds one line to stats.jsonl in your outputs folder. The kit never "
              f"sends it anywhere. Your totals: {cli_command()} stats" if stats_enabled() else
              "Usage stats are off. Nothing new is kept. A log kept so far stays in stats.jsonl in your outputs folder "
              "until you delete it.")


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
    source = urllib.parse.urlsplit(url)
    if source.hostname == "huggingface.co":
        url = url.replace("/blob/", "/resolve/")
        if "/resolve/" in url:
            return url if url.rstrip("/").endswith(file) else url.rstrip("/") + "/" + file
        repo = "/".join(source.path.strip("/").split("/")[:2])
        try:
            with open_url(f"https://huggingface.co/api/models/{repo}", headers=auth_headers(url), timeout=30) as r:
                files = [s["rfilename"] for s in json.load(r).get("siblings", [])]
        except (urllib.error.URLError, OSError, ValueError):
            return None
        hit = [f for f in files if base_name(f) == file]
        return f"https://huggingface.co/{repo}/resolve/main/{hit[0]}" if hit else None
    civ = re.search(r"civitai\.com/models/\d+.*?modelVersionId=(\d+)", url)
    if source.hostname == "civitai.com" and civ:
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
        p = (comfy_dir / c).absolute()
        if p.exists():
            return str(p)
    raise Fail(f"Can't find ComfyUI's own Python. Tell me where it is: COMFY_PYTHON=/path/to/python {cli_command()} install ...")


def install_pack(pack, custom_nodes):
    comfy_dir = custom_nodes.parent
    repo = pack.get("repo")
    if not repo:
        cmd = ["comfy", "--workspace", str(comfy_dir), "--skip-prompt", "node", "install", pack["id"]]
        print("  $", " ".join(cmd), flush=True)
        if subprocess.run(cmd).returncode == 0:
            return
        raise Fail("ComfyUI could not install the node pack from the registry.")
    name = repo.rstrip("/").rsplit("/", 1)[-1]
    dest = custom_nodes / (name[:-4] if name.endswith(".git") else name)
    if dest.parent != custom_nodes or dest.name in ("", ".", "..") or dest.is_symlink():
        raise Fail("Choose a repository with its own node pack folder.")
    if dest.exists():
        if not (dest / ".git").is_dir():
            raise Fail("Another folder already uses this node pack's name.")
        origin = subprocess.run(["git", "-C", str(dest), "remote", "get-url", "origin"], capture_output=True, text=True)
        if origin.returncode or origin.stdout.strip() != repo:
            raise Fail("The existing node pack comes from a different repository.")
    else:
        subprocess.run(["git", "clone", "--depth", "1", "--", repo, str(dest)], check=True)
    if (dest / "requirements.txt").exists():
        subprocess.run([comfy_python(comfy_dir), "-m", "pip", "install", "-r", str(dest / "requirements.txt")], check=True)
    if (dest / "install.py").exists():
        subprocess.run([comfy_python(comfy_dir), "install.py"], cwd=dest, check=True)


def cmd_install(a):
    host = urllib.parse.urlsplit(S["url"]).hostname
    if a.yes and host != "localhost":
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise Fail("Run install on the computer that runs ComfyUI, using its local address.")
    r = get_recipe(a.recipe)
    nodes, models = preflight(r.prompt())
    if not nodes and not models:
        print(f"{r.name} is ready: everything it needs is installed.")
        return
    needs = r.card.get("needs", {})
    packs = [p.copy() for p in needs.get("nodes", []) if set(p.get("classes", [])) & set(nodes)]
    unknown_nodes = [n for n in nodes if not any(n in p.get("classes", []) for p in packs)]
    by_file = {m["file"]: m for m in needs.get("models", [])}
    paths = folder_paths()
    custom_nodes = Path(paths["custom_nodes"][0])
    plan, total, todo, unknown_models = [], 0, [], []
    for p in packs:
        if p.get("id") and not re.fullmatch(r"[a-zA-Z0-9][\w.-]*", p["id"], re.ASCII):
            raise Fail("Use the registry name for the node pack.")
        if not p.get("repo"):
            if not p.get("id"):
                raise Fail("The node pack needs a registry name or repository address.")
            if not shutil.which("comfy"):
                with open_url(f"https://api.comfy.org/nodes/{p['id']}", timeout=30) as response:
                    p["repo"] = json.load(response).get("repository")
                if not p["repo"]:
                    raise Fail(f"No repository address for node pack {p['id']}.")
        plan.append(f"node pack {p.get('id')} from {p.get('repo') or 'the Comfy registry'} "
                    f"(code from the internet, goes in {custom_nodes})")
    for file in models:
        m = by_file.get(file)
        url = resolve_model_url(m) if m else None
        if not m or not url or m.get("folder") not in paths:
            unknown_models.append(file)
            continue
        root = Path(paths[m["folder"]][0]).resolve()
        dest = root / m.get("subfolder", "") / file
        if not dest.parent.resolve().is_relative_to(root) or dest.is_symlink() or dest.with_name(dest.name + ".part").is_symlink():
            raise Fail("Keep the model inside its model folder.")
        size = m.get("size") or remote_size(url)
        total += size or 0
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
        print(f"Nothing installed yet. Show this list to the person. With their OK: {cli_command()} install {r.name} --yes")
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
        print(f"New node packs load when ComfyUI starts. Restart it: {cli_command()} restart")
    else:
        print(f"Done. Check with: {cli_command()} doctor")


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
        isinstance(v, dict) and isinstance(v.get("class_type"), str) and isinstance(v.get("inputs"), dict)
        for v in wf.values())


def read_workflow(path):
    try:
        with path.open("rb") as f:
            data = f.read((16 << 20) + 1)
        if len(data) > 16 << 20:
            raise Fail(f"{path.name} is larger than 16 MiB. Save a smaller workflow.")
        return json.loads(data)
    except (ValueError, RecursionError, OSError) as e:
        raise Fail(f"Could not read {path.name} as workflow JSON ({e}).") from None


def convert_with_comfy_cli(path):
    if not shutil.which("comfy"):
        raise Fail("To turn a saved workflow into a recipe I need comfy-cli (pip install comfy-cli), or an API "
                   "export: in ComfyUI open the workflow, then Workflow menu > Export (API), and add that file "
                   f"instead (on the command line: {cli_command()} add WORKFLOW.json --api THE-EXPORT.json --name NAME)")
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


def add_recipe(src, name=None, *, into=None, force=False, api=None, title=None, same_ok=False):
    """Turn a saved workflow (or API export) into a recipe folder. Returns (name, folder, card)."""
    src = Path(src).expanduser()
    wf = read_workflow(src)
    name = slug(name or src.stem)
    folder = (Path(into).expanduser() if into else KIT / "recipes") / name
    if folder.exists() and not force:
        copy = folder / ("prompt.json" if is_api_format(wf) else "workflow.json")
        if same_ok and copy.exists() and (folder / "card.json").exists():
            canon = lambda x: json.dumps(x, sort_keys=True)
            with contextlib.suppress(Fail):
                if canon(read_workflow(copy)) == canon(wf):
                    return name, folder, read_workflow(folder / "card.json")
        raise Fail(f"A recipe called {name} already exists ({folder}). Pick another name.")
    if is_api_format(wf):
        prompt, ui = wf, None
    else:
        if not isinstance(wf, dict) or not isinstance(wf.get("nodes"), list):
            raise Fail("Choose a saved ComfyUI workflow or an API export.")
        ui = wf
        prompt = read_workflow(Path(api).expanduser()) if api else convert_with_comfy_cli(src)
    if not is_api_format(prompt):
        raise Fail("Choose a ComfyUI API export for the prompt.")
    try:
        card = draft_card(prompt, ui, title or src.stem)
        notes = notes_text(ui)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, RecursionError):
        raise Fail("A node in the workflow could not be read. Export the workflow again.") from None
    # build the whole recipe beside its final place, then move it in, so a failure leaves nothing behind
    folder.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=folder.parent))
    try:
        (stage / "prompt.json").write_text(json.dumps(prompt, indent=1), encoding="utf-8")
        (stage / "card.json").write_text(json.dumps(card, indent=1), encoding="utf-8")
        if notes:
            (stage / "notes.md").write_text(notes + "\n", encoding="utf-8")
        if ui:
            shutil.copyfile(src, stage / "workflow.json")
        if folder.exists():
            old = folder.with_name(stage.name + "-old")
            folder.rename(old)
            stage.rename(folder)
            shutil.rmtree(old, ignore_errors=True)
        else:
            stage.rename(folder)
    except OSError as e:
        shutil.rmtree(stage, ignore_errors=True)
        raise Fail(f"Could not save the recipe {name} ({e.strerror or e}).") from None
    STATS_CALL.recipe = "own"
    return name, folder, card


USEFUL_INPUT = re.compile(r"pad|strength|steps|denoise|cfg|guidance|shift|sampler|scheduler|width|height|length|"
                          r"frames|fps|megapixels|feather|grow|blend|crop|rotation|scale|upscale|count|batch", re.I)
NOISE_INPUT = re.compile(r"device|dtype|^type$|separator|on_missing|cache|^mtp$|template|delimiter|thinking|"
                         r"temperature|top_[kp]|min_p|penalty|max_length", re.I)


def other_values(folder, card, limit=24):
    """Plain node inputs the card has no setting for, as NODE.input=current, most useful first."""
    prompt = read_workflow(Path(folder) / "prompt.json")
    covered = {t for spec in card.get("inputs", {}).values() for t in targets(spec)}
    useful, rest = [], []
    for nid, node in prompt.items():
        if re.search(r"show|preview|note|save", node["class_type"], re.I):
            continue
        for field, val in node["inputs"].items():
            addr = f"{nid}.{field}"
            if addr in covered or isinstance(val, (list, dict)) or is_model_name(val) or NOISE_INPUT.search(field):
                continue
            if isinstance(val, str) and len(val) > 40:
                continue
            (useful if USEFUL_INPUT.search(field) else rest).append(f"{addr}={json.dumps(val)}")
    out = useful + rest
    return out[:limit], len(out) > limit


def cmd_add(a):
    name, folder, card = add_recipe(a.workflow, a.name, into=a.into, force=a.force, api=a.api, title=a.title)
    print(f"Made recipe {name} in {folder}")
    print(f"Settings found: {', '.join(card['inputs']) or 'none'}")
    print(f"Needs: {len(card['needs']['nodes'])} node pack(s), {len(card['needs']['models'])} model file(s)")
    rest, more = other_values(folder, card)
    if rest:
        print("Other node inputs (one-off: --set NODE.input=VALUE): " + ", ".join(rest) + (" ..." if more else ""))
    print("Next: open card.json, give the settings clear names and help text, fill in title, does and tips,\n"
          f"remove settings nobody should change, then test: {cli_command()} run {name} --count 1")


# ---------------------------------------------------------------- MCP (stdio or HTTP)

MCP_CALL_LOCK = threading.Lock()
MCP_INSTRUCTIONS = ("Makes pictures and videos with the person's ComfyUI. Call recipes to see what can run, "
                    "recipes with a name for its settings, then generate; it waits and returns the file paths. "
                    "Don't read workflow files or call ComfyUI's API yourself: these tools do that in fewer tokens.")
MCP_ADD_HINT = " A workflow file that isn't a recipe yet: recipes with add=PATH."
MCP_TOOLS = [
    {"name": "recipes",
     "description": "List the ComfyUI recipes (ready workflows) you can run. Give a name to see that recipe's settings and "
                    "tips. To use a workflow file that is not a recipe yet, give its path as add.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"},
         "add": {"type": "string", "description": "optional: path of a saved ComfyUI workflow (.json) to turn into a "
                                                  "recipe; name then names the new recipe"}}}},
    {"name": "generate",
     "description": "Run a recipe: queue count results, wait for them, return the files. Unset settings keep their "
                    "defaults. A list as a setting's value queues one job per item. To make an earlier result again "
                    "(same recipe and settings, new seed), give redo instead of recipe.",
     "inputSchema": {"type": "object", "properties": {
         "recipe": {"type": "string"},
         "redo": {"type": "string", "description": "optional: the file path of a result to make again; settings "
                                                   "given here change it"},
         "variation": {"type": "boolean", "description": "with redo: make a variation of a finished Good pick, not a redo request"},
         "by": {"type": "string", "description": "optional: the caller's display name (not a URL, token or credential)"},
         "request": {"type": "string", "description": "optional: the person's exact generation request, distinct from the image prompt"},
         "settings": {"type": "object", "description": "setting name to value, e.g. {\"prompt\": \"...\"}; a "
                                                       "NODE.input key such as \"23.steps\" sets that node input directly"},
         "count": {"type": "integer", "minimum": 1, "maximum": 16},
         "send": {"type": "string", "description": "optional: 'phone' (or another target from config) to also send them there"}}}},
    {"name": "results",
     "description": "Collect pending jobs and return their results.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def mcp_call(name, args, previews=True, *, transport="mcp-stdio"):
    buf = io.StringIO()
    done = []
    with contextlib.redirect_stdout(buf):
        if name == "recipes":
            if args.get("add"):
                if transport != "mcp-stdio":
                    raise Fail("Adding a workflow works on this computer only (the stdio server), not over the connector.")
                rname, folder, card = add_recipe(args["add"], args.get("name"), same_ok=True)
                print(f"Recipe {rname} is ready.")
                cmd_show(argparse.Namespace(recipe=rname, notes=False), registered=True)
                rest, more = other_values(folder, card)
                if rest:
                    print("Other node inputs, set in generate settings by key (e.g. {\"%s\": ...}):" % rest[0].split("=")[0])
                    print("  " + ", ".join(rest) + (" ..." if more else ""))
            elif args.get("name"):
                cmd_show(argparse.Namespace(recipe=args["name"], notes=False), registered=True)
            else:
                cmd_list(None)
        elif name == "generate":
            if args.get("send"):
                send_target(args["send"])
            r = get_recipe(args["recipe"], registered=True) if args.get("recipe") and not args.get("redo") else None
            pairs, raw = [], []
            for k, v in (args.get("settings") or {}).items():
                if r and k not in r.inputs and re.fullmatch(r"[^.\s]+\.\S+", k):
                    if transport != "mcp-stdio":
                        raise Fail(f"{r.name} has no setting '{k}'. Its settings: {', '.join(r.inputs)}", kind="bad setting")
                    if isinstance(v, (list, dict)):
                        raise Fail(f"{k} takes one plain value (text, number or true/false).", kind="bad setting")
                    raw.append((k, json.dumps(v)))  # json keeps the type: "123" stays text
                    continue
                pairs += [(k, x) for x in v] if isinstance(v, list) else [(k, v)]
            count = max(1, min(16, int(args.get("count") or 1)))
            attribution = {"transport": transport, "by": args.get("by"), "request": args.get("request")}
            variation = args.get("variation", False)
            if type(variation) is not bool or (variation and not args.get("redo")):
                raise Fail("variation must be a boolean used with redo and a finished Good pick.", kind="bad setting")
            if args.get("redo"):
                jobs = queue_redo(args["redo"], pairs, count, variation=variation, **attribution)
            elif r:
                jobs = queue_jobs(r, pairs, count, raw, **attribution)
                print(f"queued {len(jobs)} job(s) of {r.name}; run {jobs[0]['run']}")
            else:
                raise Fail("generate needs a recipe, or redo with the file of a result to make again.", kind="bad setting")
            wait = "1800" if S.get("unload_llm") else "240"
            left = wait_jobs(jobs, int(os.environ.get("COMFY_AGENT_MCP_WAIT", wait)))
            done = [j for j in jobs if j not in left]
            files = [f for j in done for f in j.get("files", [])]
            if files and args.get("send"):
                print(send_files(files, args["send"]))
            if S.get("unload_llm") and not left:
                free_vram()
            check_jobs(jobs, buf.getvalue().strip())
        elif name == "results":
            with file_lock(state_file().with_suffix(".lock")):
                pending = load_state().get("pending", [])
            if not pending:
                print("No jobs are waiting.")
            else:
                left = wait_jobs(pending, 60)
                done = [j for j in pending if j not in left]
                if S.get("unload_llm") and not left:
                    free_vram()
        else:
            raise Fail(f"unknown tool {name}")
    content = []
    if previews:
        shown = 0
        for job in done:
            for it in job.get("remote", []):
                if shown >= 4 or Path(it["filename"]).suffix.lower() not in IMAGE_EXT:
                    continue
                q = urllib.parse.urlencode({"filename": it["filename"], "subfolder": it.get("subfolder", ""),
                                            "type": it.get("type", "output"), "preview": "jpeg;70"})
                content.append({"type": "image", "mimeType": "image/jpeg",
                                "data": base64.b64encode(comfy("/view?" + q, raw=True)).decode()})
                shown += 1
    return [{"type": "text", "text": buf.getvalue().strip()}, *content]


def mcp_handle(msg, *, transport="mcp-stdio"):
    if not isinstance(msg, dict) or not isinstance(msg.get("method"), str) or not isinstance(msg.get("params", {}), dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        versions = ("2025-11-25", "2025-06-18")
        offer = msg.get("params", {}).get("protocolVersion")
        client = msg.get("params", {}).get("clientInfo")
        if transport == "mcp-stdio":  # the HTTP server can have many clients, so only an explicit `by` names one there
            MCP_CLIENT["agent"] = agent_label(client.get("name") if isinstance(client, dict) else None)
        result = {"protocolVersion": offer if offer in versions else versions[0],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "comfy-agent", "version": VERSION},
                  "instructions": MCP_INSTRUCTIONS + (MCP_ADD_HINT if transport == "mcp-stdio" else "")}
    elif method == "tools/list":
        tools = MCP_TOOLS
        if transport != "mcp-stdio":  # adding reads files on this computer, so a connector never sees it
            tools = [dict(t, description=t["description"].split(" To use a workflow file")[0],
                          inputSchema={**t["inputSchema"], "properties": {k: v for k, v in t["inputSchema"]["properties"].items() if k != "add"}})
                     if t["name"] == "recipes" else t for t in MCP_TOOLS]
        result = {"tools": tools}
    elif method == "tools/call":
        p = msg.get("params") or {}
        with MCP_CALL_LOCK, StatsCall.for_mcp(p, transport) as call:
            try:
                result = {"content": mcp_call(p.get("name"), p.get("arguments") or {}, transport=transport)}
            except (Fail, ComfyError, OSError) as e:
                result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
                stats_failed(e)
            call.reply(result)
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def cmd_mcp(a):
    if not a.http:
        out = sys.stdout
        if hasattr(out, "reconfigure"):
            out.reconfigure(encoding="utf-8")
        for line in getattr(sys.stdin, "buffer", sys.stdin):
            if not line.strip():
                continue
            try:
                msg = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
            except (ValueError, RecursionError):
                msg = None
            if msg is None:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            else:
                reply = mcp_handle(msg)
            if reply is not None:
                out.write(json.dumps(reply) + "\n")
                out.flush()
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
            reply = mcp_handle(msg, transport="mcp-http")
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
            self.wfile.flush()

        def do_GET(self):
            self.send_error(405 if self.allowed() else 404)

        def log_message(self, *args):
            pass

    print(f"MCP server on http://127.0.0.1:{a.http}/mcp/<token> (this computer only). "
          "To reach it from a chat app, see docs/chat-apps.md", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.http), Handler).serve_forever()


# ---------------------------------------------------------------- usage stats (optional, kept in the outputs folder)
#
# Off unless the person runs `config stats=on`. Then each CLI command and MCP tool call adds one JSON line to
# OUT/stats.jsonl: which command, how big the reply was, how long it took, whether it worked. Every value is a
# number or a word from a fixed list below; nothing the person or agent typed is written, and nothing is sent.

BUNDLED_RECIPES = ("klein-outpaint", "qwen-edit", "wan-i2v", "zimage")
STATS_CLI = ("list", "show", "run", "wait", "redo", "send", "doctor", "install", "restart", "add", "config")
STATS_MCP = ("recipes", "recipes show", "recipes add", "generate", "generate redo", "results")
STATS_VIA = ("cli", "mcp-stdio", "mcp-http")
STATS_ERRORS = ("model missing", "node missing", "ComfyUI not answering", "bad setting", "other")
AGENTS = {"claude code": (), "claude": ("claude ai", "claude desktop"), "codex": ("codex cli", "codex mcp client"),
          "pi": ("pi coding agent",), "openclaw": (), "cursor": ("cursor vscode",), "chatgpt": ("openai mcp",),
          "gemini": ("gemini cli", "gemini cli mcp client"), "opencode": (), "goose": (), "cline": (),
          "windsurf": ("windsurf client",), "vs code": ("visual studio code", "vscode", "copilot", "github copilot"),
          "zed": (), "lm studio": ("lmstudio",), "aider": ()}
AGENT_ALIASES = {alias: label for label, aliases in AGENTS.items() for alias in (label, *aliases)}
STATS_CALL = threading.local()  # what the current call used; reset at the start of each logged call
MCP_CLIENT = {"agent": None}  # the stdio server's one client, as it named itself when it connected


def stats_enabled():
    """Read the saved setting every time, so `config stats=off` also stops a server that is already running."""
    try:
        value = json.loads(CONFIG.read_text(encoding="utf-8")).get("stats")
    except (OSError, ValueError, AttributeError):
        return False
    return value is True or (isinstance(value, str) and value.strip().lower() == "on")


def stats_file():
    return out_dir() / "stats.jsonl"


def stats_recipe(recipe):
    """Note which recipe this call used: a recipe that comes with the kit by name, any other as "own"."""
    try:
        bundled = recipe.name in BUNDLED_RECIPES and recipe.folder.resolve() == (KIT / "recipes" / recipe.name).resolve()
        STATS_CALL.recipe = recipe.name if bundled else "own"
    except Exception:
        pass
    return recipe


def stats_failed(error):
    with contextlib.suppress(Exception):
        STATS_CALL.error = error.kind if isinstance(error, Fail) and error.kind in STATS_ERRORS else "other"


def agent_label(value):
    """A known agent's name from the name the agent gave itself, "other" for anything else."""
    if not isinstance(value, str) or not value.strip():
        return None
    return AGENT_ALIASES.get(" ".join(re.findall(r"[a-z0-9]+", value[:200].lower())), "other")


def refusal_kind(detail):
    """Sort ComfyUI's refusal of a job into one of STATS_ERRORS."""
    try:
        error = detail.get("error") or {}
        if error.get("type") == "missing_node_type" or "Cannot execute because node" in str(error.get("message", "")):
            return "node missing"
        errors = [e for ne in (detail.get("node_errors") or {}).values() for e in ne.get("errors", [])]
        if any(e.get("type") == "value_not_in_list" and re.search(r"\.(safetensors|gguf|ckpt|pt|pth|bin|sft|onnx)\b",
                                                                  str(e.get("details", "")), re.I) for e in errors):
            return "model missing"
        return "bad setting" if errors else "other"
    except Exception:
        return "other"


def stats_open(path, flags):
    """Open the log only as a plain file: never wait on a pipe or follow a link put in its place."""
    for name in ("O_NONBLOCK", "O_NOFOLLOW", "O_BINARY"):
        flags |= getattr(os, name, 0)
    fd = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"{path.name} is not a plain file")
    return fd


class Counted:
    """Pass writes through to a stream and count the characters."""

    def __init__(self, stream):
        self.stream, self.chars = stream, 0

    def write(self, text):
        self.chars += len(text) if isinstance(text, str) else 0
        return self.stream.write(text)

    def __getattr__(self, name):
        return getattr(self.stream, name)


class StatsCall:
    """Time one command or tool call and add its line to the log. Never raises."""

    def __init__(self, command, via, *, by=None, capture=False):
        self.on, self.command, self.via, self.by, self.capture = stats_enabled(), command, via, by, capture
        self.code, self.chars, self.images, self.image_bytes, self.streams = 0, 0, 0, 0, None

    @classmethod
    def for_mcp(cls, params, transport):
        name, args = params.get("name"), params.get("arguments")
        args = args if isinstance(args, dict) else {}
        label = {"recipes": "recipes add" if args.get("add") else "recipes show" if args.get("name") else "recipes",
                 "generate": "generate redo" if args.get("redo") else "generate", "results": "results"}
        return cls(label.get(name, "unknown") if isinstance(name, str) else "unknown", transport, by=args.get("by"))

    def __enter__(self):
        if self.on:
            try:
                STATS_CALL.__dict__.clear()
                self.start = time.monotonic()
                if self.capture:
                    self.streams = (sys.stdout, sys.stderr)
                    sys.stdout, sys.stderr = Counted(sys.stdout), Counted(sys.stderr)
            except Exception:
                self.on = False
        return self

    def reply(self, result):
        """Measure an MCP reply: its text, and how many pictures came with it."""
        with contextlib.suppress(Exception):
            for item in result.get("content", []):
                if item.get("type") == "text":
                    self.chars += len(item.get("text", ""))
                elif item.get("type") == "image":
                    data = item.get("data", "")
                    self.images += 1
                    self.image_bytes += len(data) * 3 // 4 - data[-2:].count("=")
            if result.get("isError"):
                self.code = 1

    def __exit__(self, kind, error, tb):
        if self.streams:
            out, err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = self.streams
            self.chars += getattr(out, "chars", 0) + getattr(err, "chars", 0)
        if kind is SystemExit:
            self.code = error.code
        elif kind is not None:
            self.code = 1
        if self.on and stats_enabled():  # asked again: stats may have been turned off during the call
            with contextlib.suppress(Exception):
                ok = self.code in (0, None)
                agent = agent_label(self.by) or getattr(STATS_CALL, "agent", None)
                if agent is None and self.via == "mcp-stdio":
                    agent = MCP_CLIENT["agent"]
                error_kind = "bad setting" if kind is SystemExit else getattr(STATS_CALL, "error", None) or "other"
                line = {"time": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "kit": VERSION,
                        "command": self.command, "via": self.via, "recipe": getattr(STATS_CALL, "recipe", None),
                        "agent": agent, "reply_chars": self.chars, "reply_tokens_est": (self.chars + 3) // 4,
                        "images": self.images, "image_bytes": self.image_bytes,
                        "seconds": round(time.monotonic() - self.start, 1), "ok": ok, "error": None if ok else error_kind}
                stats_file().parent.mkdir(parents=True, exist_ok=True)
                fd = stats_open(stats_file(), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
                try:
                    os.write(fd, (json.dumps(line) + "\n").encode())
                finally:
                    os.close(fd)
        return False


def stats_row(raw):
    """One log line, every value checked against the fixed lists, so a summary can only hold those words."""
    try:
        row = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(row, dict) or not isinstance(row.get("command"), str) or type(row.get("ok")) is not bool:
        return None

    def number(key, top):
        value = row.get(key)
        return value if type(value) in (int, float) and 0 <= value <= top else 0

    def word(key, allowed, missing=None):
        value = row.get(key)
        return missing if value is None else value if isinstance(value, str) and value in allowed else "other"

    try:
        when = dt.datetime.strptime(str(row.get("time"))[:20], "%Y-%m-%dT%H:%M:%SZ").date()
    except ValueError:
        when = None
    return {"time": when, "command": word("command", STATS_CLI + STATS_MCP + ("marks", "gallery", "unknown")),
            "via": word("via", STATS_VIA, "other"), "recipe": word("recipe", BUNDLED_RECIPES + ("own",)),
            "agent": word("agent", (*AGENTS, "other")), "tokens": number("reply_tokens_est", 10 ** 9),
            "images": number("images", 10 ** 4), "image_bytes": number("image_bytes", 10 ** 12),
            "seconds": number("seconds", 10 ** 7), "ok": row["ok"],
            "error": None if row["ok"] else word("error", STATS_ERRORS, "other")}


def stats_rows():
    """The usage log's lines; None when it is there but can't be read."""
    try:
        fd = stats_open(stats_file(), os.O_RDONLY)
    except FileNotFoundError:
        return []
    except OSError:
        return None
    try:
        with os.fdopen(fd, encoding="utf-8", errors="replace") as f:
            return [row for row in map(stats_row, f) if row]
    except OSError:
        return None


def stats_summary(rows):
    """A few plain lines of totals: numbers, and words from the fixed lists."""
    def listed(counter):
        return ", ".join(f"{k} {n:,}" for k, n in counter.most_common())

    def median(values):
        return statistics.median(list(values))

    agents = collections.Counter(r["agent"] for r in rows if r["agent"])
    system = {"Darwin": "macOS"}.get(platform.system(), platform.system()) or "unknown system"
    head = [f"comfy-agent {VERSION}", system,
            "agent: " + (", ".join(a for a, _ in agents.most_common(3)) + " (as reported)" if agents else "not reported")]
    days = sorted({r["time"] for r in rows if r["time"]})
    if days:
        span = (days[-1] - days[0]).days + 1
        head.append(f"{span} day" + "s" * (span != 1) + (f", used on {len(days)}" if len(days) != span else ""))
    worked = sum(r["ok"] for r in rows)
    via = collections.Counter("cli" if r["via"] == "cli" else "mcp" if r["via"].startswith("mcp") else "other" for r in rows)
    biggest = max(rows, key=lambda r: r["tokens"])
    lines = [" · ".join(head),
             f"calls {len(rows):,} · worked {worked:,} · failed {len(rows) - worked:,} · {listed(via)}",
             f"reply tokens (estimated): median {round(median(r['tokens'] for r in rows)):,}, "
             f"largest {round(biggest['tokens']):,} ({biggest['command']})"]
    tools = collections.defaultdict(list)
    for r in rows:
        tools[r["command"]].append(r)
    for name, group in sorted(tools.items(), key=lambda item: -len(item[1])):
        lines.append(f"  {name}: {len(group):,} call" + "s" * (len(group) != 1)
                     + f", median {round(median(r['tokens'] for r in group)):,},"
                       f" largest {round(max(r['tokens'] for r in group)):,},"
                       f" about {median(r['seconds'] for r in group):.1f} s each")
    images = sum(r["images"] for r in rows)
    if images:
        size = sum(r["image_bytes"] for r in rows)
        lines.append(f"pictures in MCP replies: {round(images):,}, "
                     + (f"{size / 1e6:.1f} MB" if size >= 1e5 else f"{size / 1e3:.0f} KB"))
    failures = collections.Counter(r["error"] for r in rows if not r["ok"])
    if failures:
        lines.append("failures: " + listed(failures))
    recipes = collections.Counter(r["recipe"] for r in rows if r["recipe"])
    if recipes:
        lines.append("recipes: " + listed(recipes))
    return "\n".join(lines) + "\n"


def cmd_stats(a):
    rows = stats_rows()
    state = "on" if stats_enabled() else "off"
    where = "stats.jsonl in your outputs folder"
    if rows is None:
        raise Fail(f"Can't read the usage log ({where}). Usage stats are {state}.")
    if not rows:
        print(f"Usage stats are {state}. Nothing is kept yet." + (" No share file was written." if a.share else "")
              + ("" if state == "on" else " To keep a private log of how much the kit adds to your agent's tokens: "
                 f"{cli_command()} config stats=on"))
        return
    summary = stats_summary(rows)
    if not a.share:
        print(f"Usage stats are {state}. The log is {where}; the kit never sends it anywhere.")
        print(summary, end="")
        switch = f"Stop: {cli_command()} config stats=off" if state == "on" else f"Turn back on: {cli_command()} config stats=on"
        print(f"A summary you can post yourself: {cli_command()} stats --share. {switch}. "
              f"To delete the log, delete that file ({cli_command()} config shows the outputs folder).")
        return
    fd, tmp = tempfile.mkstemp(dir=out_dir(), prefix=".stats-share-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(summary)
        os.replace(tmp, out_dir() / "stats-share.txt")  # replaces a link there, never writes through it
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    print(summary)
    print("That's the whole file: stats-share.txt in your outputs folder. The kit never uploads it. If you'd like to "
          "help improve the kit, you can post it yourself on a GitHub issue (github.com/ausboss/comfy-agent) or in Discord.")


# ---------------------------------------------------------------- main

def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    ap = argparse.ArgumentParser(prog="comfy_agent.py", description="Make pictures and videos with ComfyUI recipes.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the recipes")
    p = sub.add_parser("show", help="a recipe's settings")
    p.add_argument("recipe")
    p.add_argument("--notes", action="store_true", help="also print the workflow author's notes")
    helps = {"run": "queue a recipe and save results", "wait": "collect queued jobs",
             "redo": "make a result again: same recipe and settings, a new seed"}
    for name, text in helps.items():
        p = sub.add_parser(name, help=text)
        if name == "run":
            p.add_argument("recipe")
        if name == "redo":
            p.add_argument("file", help="a result to make again")
            p.add_argument("--variation", action="store_true", help="make a variation of a finished Good pick, not a requested redo")
        if name != "wait":
            p.add_argument("values", nargs="*", help="setting=value; repeat a setting to queue one job per value")
            p.add_argument("--by", help="optional caller display name for the receipt, not a URL or credential")
            p.add_argument("--request", help="optional exact generation request, distinct from the picture prompt")
            p.add_argument("--count", "-n", type=int, default=1, help="results per setting combination")
            p.add_argument("--no-wait", action="store_true", help="queue and return at once")
        if name == "run":
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
    p = sub.add_parser("config", help="show or change settings")
    p.add_argument("values", nargs="*", help="url=..., out=..., unload_llm=COMMAND, phone=tailscale:DEVICE or phone=ntfy:TOPIC")
    p = sub.add_parser("mcp", help="run as an MCP server")
    p.add_argument("--http", type=int, help="serve Streamable HTTP on this port instead of stdio")
    p.add_argument("--token", help="secret for the HTTP server")
    p = sub.add_parser("stats", help="your own usage totals, if you turned stats on (config stats=on)")
    p.add_argument("--share", action="store_true", help="write a short summary you can post yourself")
    args = sys.argv[1:] if argv is None else list(argv)
    command = next((x for x in args if not x.startswith("-")), None)
    if command in ("mcp", "stats"):  # mcp logs each tool call; stats is the person's own look
        return run_command(ap, args)
    with StatsCall(command if command in STATS_CLI else "unknown", "cli", capture=True) as call:
        call.code = run_command(ap, args)
    return call.code


def run_command(ap, args):
    a = ap.parse_args(args)
    with contextlib.suppress(Exception):
        STATS_CALL.agent = agent_label(getattr(a, "by", None))
    try:
        globals()["cmd_" + a.cmd](a)
    except (Fail, OSError) as e:
        stats_failed(e)
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
