#!/usr/bin/env python3
"""A scripted OpenAI-compatible chat server for probing Pi without a GPU.

Every request is appended to --log as JSON. Each POST to /v1/chat/completions takes the next step from
--script, a list separated by ",," (JSON args may hold commas):
  tool:NAME:JSONARGS   answer with one tool call (e.g. tool:mcp__slow__sleep:{"seconds":90})
  text:WORDS           answer with plain text
  notloaded            answer HTTP 400 exactly like llama.cpp's router with --no-models-autoload
  unavailable          answer HTTP 503 "Loading model" like a single-model llama-server still loading
After the script runs out it keeps answering text:ok.
"""
import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=18080)
ap.add_argument("--script", default="text:ok")
ap.add_argument("--log", default="fake_llm.jsonl")
ap.add_argument("--model", default="local-test")
a = ap.parse_args()

steps = []
for part in a.script.split(",,"):
    steps.append(part)
step_i = 0


def log(rec):
    rec["t"] = time.time()
    with open(a.log, "a") as f:
        f.write(json.dumps(rec) + "\n")


def chunk(delta, finish=None):
    return {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": a.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        log({"method": "GET", "path": self.path})
        if self.path.startswith("/v1/models") or self.path.startswith("/models"):
            self.send_json(200, {"object": "list", "data": [{"id": a.model, "object": "model"}]})
        else:
            self.send_json(200, {"status": "ok"})

    def do_POST(self):
        global step_i
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        step = steps[step_i] if step_i < len(steps) else "text:ok"
        step_i += 1
        msgs = req.get("messages", [])
        last = msgs[-1] if msgs else {}
        log({"method": "POST", "path": self.path, "step": step, "model": req.get("model"),
             "stream": req.get("stream"), "n_messages": len(msgs),
             "last_role": last.get("role"), "last_content": last.get("content"),
             "tools": [t.get("function", {}).get("name") for t in req.get("tools", [])]})
        if step == "notloaded":
            return self.send_json(400, {"error": {"code": 400, "message": "model is not loaded",
                                                  "type": "invalid_request_error"}})
        if step == "unavailable":
            return self.send_json(503, {"error": {"code": 503, "message": "Loading model",
                                                  "type": "unavailable_error"}})
        kind, _, rest = step.partition(":")
        if kind == "tool":
            name, _, args = rest.partition(":")
            delta = {"role": "assistant", "content": None, "tool_calls": [
                {"index": 0, "id": f"call_{step_i}", "type": "function",
                 "function": {"name": name, "arguments": args or "{}"}}]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": rest or "ok"}
            finish = "stop"
        if not req.get("stream"):
            msg = {k: v for k, v in delta.items()}
            return self.send_json(200, {"id": "c1", "object": "chat.completion", "model": a.model,
                                        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for obj in (chunk(delta), chunk({}, finish)):
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
        usage = {"id": "c1", "object": "chat.completion.chunk", "model": a.model, "choices": [],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        self.wfile.write(f"data: {json.dumps(usage)}\n\ndata: [DONE]\n\n".encode())
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, *args):
        pass


print(f"fake llm on :{a.port}", file=sys.stderr, flush=True)
ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
