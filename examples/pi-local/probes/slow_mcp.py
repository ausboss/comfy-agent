#!/usr/bin/env python3
"""A stdio MCP server with one slow tool, built like comfy_agent.py's server (one request at a time).

Tool `sleep(seconds)` blocks for that long. With --progress N it sends notifications/progress every N
seconds when the client gave a progressToken. Every message in and out is appended to --log.
"""
import argparse
import json
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("--log", default="slow_mcp.jsonl")
ap.add_argument("--progress", type=float, default=0)
a = ap.parse_args()


def log(rec):
    rec["t"] = time.time()
    with open(a.log, "a") as f:
        f.write(json.dumps(rec) + "\n")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
    log({"out": obj})


TOOLS = [{"name": "sleep", "description": "Wait the given number of seconds, like a long render.",
          "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}}]

for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    log({"in": msg})
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        continue  # notifications (initialized, cancelled) are only logged, like comfy_agent.py
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}}, "serverInfo": {"name": "slow", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        p = msg.get("params") or {}
        secs = float((p.get("arguments") or {}).get("seconds", 1))
        token = (p.get("_meta") or {}).get("progressToken")
        start = time.time()
        while time.time() - start < secs:
            step = min(a.progress or secs, secs - (time.time() - start))
            time.sleep(max(step, 0))
            if a.progress and token is not None and time.time() - start < secs:
                send({"jsonrpc": "2.0", "method": "notifications/progress",
                      "params": {"progressToken": token, "progress": round(time.time() - start),
                                 "message": "still rendering"}})
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": f"slept {secs:g} s"}]}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}})
