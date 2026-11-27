#!/usr/bin/env python3
"""A whole fake stack for a dry run of cycle_driver.py with the real Pi and the real kit, no GPU.

  python3 sim_stack.py --kit /path/to/comfy-agent --pi-dir /tmp/pi-agent

Starts the kit's own test fakes (tests/fakes.py: ComfyUI that renders in time and keeps memory books, and a
llama.cpp router with --no-models-autoload) and adds /v1/chat/completions to the fake router: a scripted
"model" that refuses with 400 while unloaded (like the real router), calls mcp__comfy__generate when asked
for a picture, and repeats the seed from the tool result when asked for it. It writes a test recipe, the
kit's comfy-agent.local.json (in --kit: use a scratch copy of the kit) and Pi's models.json/mcp.json in
--pi-dir, prints the driver command, and serves until killed. Results are simulated.
"""
import argparse
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--kit", required=True)
ap.add_argument("--pi-dir", required=True)
ap.add_argument("--render", type=float, default=3.0, help="seconds per fake render")
a = ap.parse_args()
kit = Path(a.kit).resolve()
sys.path.insert(0, str(kit / "tests"))
import fakes  # noqa: E402

world = fakes.World()
world.render_time = a.render
comfy_h, router_h, _ = fakes.make_handler(world)
MODEL = "qwen"
state = {"seed": None, "requests": 0, "refused": 0}


class Router(router_h):
    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            return super().do_POST()
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        state["requests"] += 1
        with world.lock:
            loaded = world.llm_loaded.get(req.get("model")) is True
        if not loaded:
            state["refused"] += 1
            return self.reply({"error": {"code": 400, "message": "model is not loaded",
                                         "type": "invalid_request_error"}}, 400)
        last = (req.get("messages") or [{}])[-1]
        content = last.get("content")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        content = content or ""
        delta, finish = {"role": "assistant", "content": "ok"}, "stop"
        if last.get("role") == "tool":
            m = re.search(r"seed (\d+)", content)
            state["seed"] = m.group(1) if m else None
            path = re.search(r"(\S+\.png)", content)
            delta["content"] = f"Done: {path.group(1) if path else '?'}, seed {state['seed']}"
        elif "What seed" in content:
            delta["content"] = str(state["seed"])
        elif "Make one picture of" in content:
            subject = re.search(r"picture of (.+?) with", content).group(1)
            recipe = re.search(r"using the (\S+) recipe", content).group(1)
            delta = {"role": "assistant", "content": None, "tool_calls": [{
                "index": 0, "id": f"call_{state['requests']}", "type": "function",
                "function": {"name": "mcp__comfy__generate",
                             "arguments": json.dumps({"recipe": recipe, "settings": {"prompt": subject}})}}]}
            finish = "tool_calls"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for d, f in ((delta, None), ({}, finish)):
            chunk = {"id": "c", "object": "chat.completion.chunk", "model": MODEL,
                     "choices": [{"index": 0, "delta": d, "finish_reason": f}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


_, comfy_url = fakes.serve(comfy_h)
_, router_url = fakes.serve(Router)
recipes = kit / "sim-recipes" / "test"
recipes.mkdir(parents=True, exist_ok=True)
(recipes / "card.json").write_text(json.dumps(fakes.CARD))
(recipes / "prompt.json").write_text(json.dumps(fakes.PROMPT))
(kit / "comfy-agent.local.json").write_text(json.dumps({
    "url": comfy_url, "recipes": [str(recipes.parent)], "out": str(kit / "sim-out"),
    "inference": {"type": "llama-router", "url": router_url, "model": MODEL}}, indent=1))
pi = Path(a.pi_dir)
pi.mkdir(parents=True, exist_ok=True)
(pi / "models.json").write_text(json.dumps({"providers": {"local": {
    "baseUrl": router_url + "/v1", "api": "openai-completions", "apiKey": "none",
    "models": [{"id": MODEL, "contextWindow": 32768, "maxTokens": 1024,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}, indent=1))
(pi / "mcp.json").write_text(json.dumps({"mcpServers": {"comfy": {
    "command": sys.executable, "args": [str(kit / "comfy_agent.py"), "mcp"], "exposure": "direct",
    "timeout": 120}}}, indent=1))
print(json.dumps({"comfy": comfy_url, "llm": router_url, "model": MODEL,
                  "driver": f"PI_CODING_AGENT_DIR={pi} python3 cycle_driver.py --kit {kit} --comfy {comfy_url} "
                            f"--llm {router_url} --model {MODEL} --recipe test --out RUN"}), flush=True)
try:
    while True:
        time.sleep(5)
        with world.lock:
            if world.violations:
                print(json.dumps({"violations": world.violations}), flush=True)
                world.violations.clear()
except KeyboardInterrupt:
    pass
finally:
    print(json.dumps({"chat_requests": state["requests"], "refused_while_unloaded": state["refused"],
                      "submits": len(world.submits)}), flush=True)
