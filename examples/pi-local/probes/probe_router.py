#!/usr/bin/env python3
"""Check a running llama.cpp router against what the handoff relies on, and time load/unload.

  python3 probe_router.py --llm http://127.0.0.1:8080 --model qwen38-27b [--comfy http://127.0.0.1:8199] [--rounds 3]

Needs the router started by start-router.sh (--no-models-autoload). Steps, each printed as one line:
  1. GET /models lists the model; /props reports models_autoload false
  2. unload (if loaded), then a chat request must get 400 "model is not loaded" (no autoload)
  3. rounds x: load -> wait for "loaded" -> one short chat -> unload -> wait for "unloaded",
     timing each and reading free memory from ComfyUI /system_stats and nvidia-smi when present
It leaves the model loaded at the end (as Pi expects). Uses the GPU: check nothing else is running first.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request


def req(url, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                               method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


def status(a):
    _, m = req(a.llm + "/models")
    for x in (m or {}).get("data", []):
        if x.get("id") == a.model:
            return (x.get("status") or {}).get("value"), x.get("status")
    return None, None


def wait_state(a, want, limit):
    t0 = time.time()
    while time.time() - t0 < limit:
        s, full = status(a)
        if s == want:
            return round(time.time() - t0, 1), full
        if full and full.get("failed"):
            return None, full
        time.sleep(0.25)
    return None, status(a)[1]


def free_mb(a):
    out = {}
    if a.comfy:
        _, st = req(a.comfy + "/system_stats", timeout=5)
        if st and st.get("devices"):
            out["comfy_free_mb"] = st["devices"][0].get("vram_free", 0) // 2 ** 20
    if shutil.which("nvidia-smi"):
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True)
        used, total = [int(x) for x in r.stdout.splitlines()[0].split(",")]
        out["nv_used_mb"], out["nv_total_mb"] = used, total
    return out


def chat(a):
    t0 = time.time()
    code, body = req(a.llm + "/v1/chat/completions", {"model": a.model, "max_tokens": 8,
                                                      "messages": [{"role": "user", "content": "Say ok."}]}, 300)
    return code, body, round(time.time() - t0, 1)


def line(**kw):
    print(json.dumps(kw), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", default="http://127.0.0.1:8080")
    ap.add_argument("--model", default="qwen38-27b")
    ap.add_argument("--comfy", default="")
    ap.add_argument("--rounds", type=int, default=3)
    a = ap.parse_args()
    ok = True
    _, props = req(a.llm + "/props?autoload=false")
    s, full = status(a)
    line(step="listed", model=a.model, status=s, models_autoload=(props or {}).get("models_autoload"))
    if s is None:
        sys.exit(f"{a.model} is not in GET /models")
    if s in ("loaded", "loading", "sleeping"):
        req(a.llm + "/models/unload", {"model": a.model})
        t, _ = wait_state(a, "unloaded", 120)
        line(step="initial_unload", seconds=t, **free_mb(a))
    code, body, _ = chat(a)
    refused = code == 400 and "not loaded" in json.dumps(body)
    ok &= refused
    line(step="chat_while_unloaded", http=code, body=body, as_expected=refused, **free_mb(a))
    for i in range(a.rounds):
        before = free_mb(a)
        t0 = time.time()
        code, body = req(a.llm + "/models/load", {"model": a.model}, 30)
        t_load, full = wait_state(a, "loaded", 600)
        loaded = free_mb(a)
        ccode, cbody, t_chat = chat(a)
        last = i == a.rounds - 1
        line(step="load", round=i + 1, http=code, seconds=t_load, failed=(full or {}).get("failed"),
             chat_http=ccode, first_chat_s=t_chat, before=before, loaded=loaded)
        ok &= t_load is not None and ccode == 200
        if last:
            break
        req(a.llm + "/models/unload", {"model": a.model})
        t_un, _ = wait_state(a, "unloaded", 120)
        time.sleep(1)
        line(step="unload", round=i + 1, seconds=t_un, after=free_mb(a))
        ok &= t_un is not None
    line(step="result", ok=ok, left="loaded")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
