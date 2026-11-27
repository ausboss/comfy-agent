#!/usr/bin/env python3
"""Probe how a real Pi build handles a long MCP tool call and an unloaded local model.

No GPU and no real model: Pi talks to fake_llm.py (scripted answers) and slow_mcp.py (a tool that blocks
like comfy_agent.py's generate). Results are simulated backend, real Pi. Usage:

  python3 probe_pi.py --pi /path/to/pi [--only timeout,notloaded,...] [--out DIR]

Each scenario prints one summary line and leaves its logs in --out/SCENARIO/.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

SCENARIOS = {
    # name: (mcp timeout s, tool sleep s, progress every s, llm script, prompt)
    "timeout": (10, 30, 0, 'tool:mcp__slow__sleep:{"seconds":30},,text:done', "render it"),
    "progress": (10, 30, 5, 'tool:mcp__slow__sleep:{"seconds":30},,text:done', "render it"),
    "notloaded": (10, 30, 0, 'tool:mcp__slow__sleep:{"seconds":30},,notloaded', "render it"),
    "queued": (10, 30, 0, 'tool:mcp__slow__sleep:{"seconds":30},,tool:mcp__slow__sleep:{"seconds":1},,text:done',
               "render it"),
    "loading503": (60, 1, 0, 'unavailable,,unavailable,,text:done', "hello"),
    "notloaded_first": (60, 1, 0, 'notloaded,,text:done', "hello"),
}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def read_jsonl(p):
    if not Path(p).exists():
        return []
    return [json.loads(x) for x in Path(p).read_text().splitlines() if x.strip()]


def run(name, pi, out, deadline):
    mcp_timeout, sleep_s, progress, script, prompt = SCENARIOS[name]
    d = out / name
    shutil.rmtree(d, ignore_errors=True)
    agent = d / "agent"
    agent.mkdir(parents=True)
    port = free_port()
    (agent / "models.json").write_text(json.dumps({"providers": {"local": {
        "baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions", "apiKey": "none",
        "models": [{"id": "local-test", "contextWindow": 32768, "maxTokens": 1024,
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}, indent=1))
    mcp_args = [str(HERE / "slow_mcp.py"), "--log", str(d / "mcp.jsonl")]
    if progress:
        mcp_args += ["--progress", str(progress)]
    (agent / "mcp.json").write_text(json.dumps({"mcpServers": {"slow": {
        "command": sys.executable, "args": mcp_args, "exposure": "direct", "timeout": mcp_timeout}}}, indent=1))
    llm = subprocess.Popen([sys.executable, str(HERE / "fake_llm.py"), "--port", str(port), "--script", script,
                            "--log", str(d / "llm.jsonl")], stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    env = {**os.environ, "PI_CODING_AGENT_DIR": str(agent), "PI_OFFLINE": "1"}
    for k in list(env):
        if k.endswith("_API_KEY") or k in ("ANTHROPIC_AUTH_TOKEN",):
            env.pop(k)  # so a missing local model cannot quietly fall back to a cloud provider we hold a key for
    t0 = time.time()
    p = subprocess.Popen([pi, "--mode", "rpc", "--no-session", "--provider", "local", "--model", "local-test"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(d / "pi.stderr", "w"),
                         env=env, cwd=d)
    p.stdin.write((json.dumps({"id": "p1", "type": "prompt", "message": prompt}) + "\n").encode())
    p.stdin.flush()
    events = []
    settled = False
    with open(d / "pi.jsonl", "w") as f:
        while time.time() - t0 < deadline:
            line = p.stdout.readline()
            if not line:
                break
            f.write(line.decode())
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            ev["_t"] = round(time.time() - t0, 2)
            events.append(ev)
            if ev.get("type") == "agent_settled":
                settled = True
                break
    settle_t = round(time.time() - t0, 1)
    # leave Pi alive a little longer so a late tool result (after a timeout) still reaches the MCP log
    time.sleep(max(0, sleep_s - settle_t + 3) if name in ("timeout", "notloaded", "queued") else 0.5)
    p.stdin.close()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()
    llm.terminate()
    return summarize(name, d, events, settled, settle_t)


def summarize(name, d, events, settled, settle_t):
    llm = [r for r in read_jsonl(d / "llm.jsonl") if r.get("method") == "POST"]
    mcp = read_jsonl(d / "mcp.jsonl")
    tools = [(e.get("toolName"), e.get("isError"), e.get("durationMs"),
              json.dumps(e.get("result", {}).get("content", ""))[:160])
             for e in events if e.get("type") == "tool_execution_end"]
    msgs = [e["message"] for e in events if e.get("type") == "message_end" and e.get("message", {}).get("role") == "assistant"]
    providers = sorted({f"{m.get('provider')}/{m.get('model')}" for m in msgs})
    errors = [m.get("errorMessage") for m in msgs if m.get("stopReason") == "error"]
    retries = [e.get("type") for e in events if "retry" in str(e.get("type"))]
    cancels = [r["in"].get("params") for r in mcp if "in" in r and r["in"].get("method") == "notifications/cancelled"]
    calls = [r for r in mcp if "in" in r and r["in"].get("method") == "tools/call"]
    late = [r for r in mcp if "out" in r and "result" in r["out"] and "content" in r["out"]["result"]]
    summary = {
        "scenario": name, "settled": settled, "settled_after_s": settle_t,
        "llm_requests": len(llm), "llm_steps": [r["step"][:30] for r in llm],
        "tool_results": tools, "assistant_providers": providers, "assistant_errors": errors,
        "retry_events": retries, "mcp_tool_calls": len(calls), "mcp_cancel_notifications": cancels,
        "mcp_results_sent": len(late),
        "tool_result_seen_by_model": [str(r.get("last_content"))[:200] for r in llm if r.get("last_role") == "tool"],
    }
    (d / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pi", default=shutil.which("pi") or "pi")
    ap.add_argument("--only", default=",".join(SCENARIOS))
    ap.add_argument("--out", default=str(Path.cwd() / "probe-out"))
    ap.add_argument("--deadline", type=float, default=120)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ver = subprocess.run([a.pi, "--version"], capture_output=True, text=True).stdout.strip()
    print(f"pi {ver}")
    for name in a.only.split(","):
        print(json.dumps(run(name, a.pi, out, a.deadline)), flush=True)


if __name__ == "__main__":
    main()
