#!/usr/bin/env python3
"""Drive Pi through N chat -> render -> resume cycles on a real machine and record the evidence.

  python3 cycle_driver.py --cycles 20 --out runs/arch3090-$(date +%Y%m%d-%H%M) \
      --kit /path/to/comfy-agent --comfy http://127.0.0.1:8199 --llm http://127.0.0.1:8080

Before the first cycle it checks the setup (nothing is loaded or changed): ComfyUI answers and is idle,
the model server is in router mode with the model loaded, and Pi answers from the local provider.
It then starts vram_sampler.py, and for each cycle:
  1. new Pi session (RPC mode), prompt: make one zimage picture of SUBJECT and say the seed
  2. follow-up in the same session: what seed did you use? (needs the model back after the render)
  3. records: Pi tool calls and their duration, every assistant message's provider/model (plan A12),
     the kit's operation record (`ops --json`: phase timings, free memory), files on disk
A cycle passes when: generate ran once without error, a file exists, the follow-up names that seed,
and every assistant message came from the local provider. The sampler's summary adds the residency check.

Outputs in --out: run.json (versions and settings), cycles.jsonl, markers.jsonl, vram.jsonl,
pi-NN.jsonl (raw Pi events), summary.json. `--pilot` is shorthand for --cycles 2.
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUBJECTS = ["a red fox asleep in the snow", "a lighthouse on a cliff at dusk", "a bowl of ramen on a wooden table",
            "an old bicycle against a blue wall", "a heron standing in a misty pond"]
EDITS = ["make it night time with a full moon", "make it snow", "turn it into a watercolor painting",
         "make it golden hour", "add fog"]


def get_json(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except Exception as e:
        return {"_error": str(e)}


def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           shell=isinstance(cmd, str))
        return (r.stdout or r.stderr).strip()
    except Exception as e:
        return f"unavailable: {e}"


def mark(out, event, **kw):
    with open(out / "markers.jsonl", "a") as f:
        f.write(json.dumps({"t": time.time(), "event": event, **kw}) + "\n")


class Pi:
    """One Pi RPC process = one session."""

    def __init__(self, a, log):
        cmd = [a.pi, "--mode", "rpc", "--no-session"]
        if a.provider:
            cmd += ["--provider", a.provider]
        if a.model:
            cmd += ["--model", a.model]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=open(str(log) + ".stderr", "a"), cwd=a.workdir)
        self.log = open(log, "a")
        self.n = 0

    def prompt(self, text, deadline):
        self.n += 1
        self.p.stdin.write((json.dumps({"id": f"p{self.n}", "type": "prompt", "message": text}) + "\n").encode())
        self.p.stdin.flush()
        events, t0 = [], time.time()
        while time.time() - t0 < deadline:
            line = self.p.stdout.readline()
            if not line:
                break
            self.log.write(line.decode(errors="replace"))
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            ev["_t"] = time.time()
            events.append(ev)
            if ev.get("type") == "agent_settled":
                break
        return events, round(time.time() - t0, 1)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(20)
        except Exception:
            self.p.kill()


def assistant_msgs(events):
    return [e["message"] for e in events
            if e.get("type") == "message_end" and (e.get("message") or {}).get("role") == "assistant"]


def text_of(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c or [] if b.get("type") == "text")


def ops_json(a):
    if not a.kit:
        return None
    out = sh([sys.executable, str(Path(a.kit) / "comfy_agent.py"), "ops", "--json"], timeout=30)
    try:
        data = json.loads(out)
        return data[0] if isinstance(data, list) and data else data
    except ValueError:
        return {"_raw": out[:500]}


def preflight(a):
    problems = []
    st = get_json(a.comfy + "/system_stats")
    q = get_json(a.comfy + "/queue")
    if "_error" in st:
        problems.append(f"ComfyUI not answering at {a.comfy}: {st['_error']}")
    elif q.get("queue_running") or q.get("queue_pending"):
        problems.append("ComfyUI is busy: wait for its queue to empty (someone else's GPU work?)")
    m = get_json(a.llm + "/models")
    states = {x.get("id"): (x.get("status") or {}).get("value") for x in (m.get("data") or [])} if "_error" not in m else {}
    if "_error" in m:
        problems.append(f"model server not answering at {a.llm}: {m['_error']}")
    elif not any(v == "loaded" for v in states.values()):
        problems.append(f"no model loaded on the router ({states}); load it first: "
                        f"curl -X POST {a.llm}/models/load -d '{{\"model\": \"NAME\"}}'")
    return problems, st, states


def record_env(a, st, states):
    env = {
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": platform.node(), "os": platform.platform(),
        "python": platform.python_version(),
        "gpu": sh("nvidia-smi --query-gpu=name,memory.total,driver_version,pcie.link.gen.max --format=csv,noheader")
        if shutil.which("nvidia-smi") else "no nvidia-smi",
        "comfyui": (st.get("system") or {}) if isinstance(st, dict) else None,
        "comfy_devices": (st.get("devices") if isinstance(st, dict) else None),
        "pi": sh([a.pi, "--version"]),
        "llama_server": sh([a.llama_server, "--version"]) if a.llama_server else "not given",
        "router_models": states,
        "router_props": get_json(a.llm + "/props?autoload=false" + (f"&model={a.model}" if a.model else "")),
        "kit_commit": sh(["git", "-C", a.kit, "rev-parse", "--short", "HEAD"]) if a.kit else None,
        "kit_inference_config": sh([sys.executable, str(Path(a.kit) / "comfy_agent.py"), "config"]) if a.kit else None,
        "model_file": a.model_file, "context": a.context, "recipe": a.recipe, "cycles": a.cycles,
        "notes": a.notes,
    }
    if a.model_file and os.path.exists(a.model_file):
        env["model_file_bytes"] = os.path.getsize(a.model_file)
    return env


def run_cycle(a, i, out):
    subject = SUBJECTS[i % len(SUBJECTS)]
    mark(out, "cycle_start", cycle=i)
    pi = Pi(a, out / f"pi-{i:02d}.jsonl")
    if a.image:  # an edit recipe (e.g. qwen-edit) on the person's own picture
        subject = EDITS[i % len(EDITS)]
        ask = (f"Edit my picture {a.image} with my ComfyUI, using the {a.recipe} recipe: {subject}. "
               "Make one result. When it is done, tell me the file path and the seed.")
    else:
        ask = (f"Make one picture of {subject} with my ComfyUI, using the {a.recipe} recipe. "
               "When it is done, tell me the file path and the seed.")
    ev1, t1 = pi.prompt(ask, a.turn_deadline)
    mark(out, "render_turn_end", cycle=i)
    ev2, t2 = pi.prompt("What seed did that picture use? Answer with only the number.", 300)
    pi.close()
    mark(out, "cycle_end", cycle=i)
    tools = [e for e in ev1 if e.get("type") == "tool_execution_end"]
    gens = [e for e in tools if str(e.get("toolName", "")).endswith("generate")]
    msgs = assistant_msgs(ev1) + assistant_msgs(ev2)
    providers = sorted({f"{m.get('provider')}/{m.get('model')}" for m in msgs})
    errors = [m.get("errorMessage") for m in msgs if m.get("stopReason") == "error"]
    op = ops_json(a)
    seeds = [str(j.get("seed")) for j in (op or {}).get("jobs", []) if isinstance(op, dict)]
    files = [f for j in (op or {}).get("jobs", []) for f in (j.get("files") or [])] if isinstance(op, dict) else []
    answer = text_of(msgs[-1]) if msgs else ""
    nums = re.findall(r"\d{3,}", answer)
    checks = {
        "generate_once_ok": len(gens) == 1 and not gens[0].get("isError"),
        "file_exists": bool(files) and all(os.path.exists(f) or os.path.exists(Path(a.kit or ".") / f) for f in files),
        "followup_seed_matches": bool(seeds) and any(n in seeds for n in nums),
        "all_local": bool(msgs) and all(p.startswith(a.expect_provider + "/") for p in providers),
        "no_assistant_errors": not errors,
        # the kit's runner notes "intrusion: ..." when something loaded the model while ComfyUI had the GPU
        "no_intrusion": not any("intrusion" in str(n) for n in ((op or {}).get("notes") or [] if isinstance(op, dict) else [])),
    }
    rec = {"cycle": i, "subject": subject, "pass": all(checks.values()), "checks": checks,
           "render_turn_s": t1, "followup_turn_s": t2, "tool_calls": [(e.get("toolName"), e.get("isError"),
                                                                       e.get("durationMs")) for e in tools],
           "providers": providers, "errors": errors, "answer": answer[:200], "op": op}
    with open(out / "cycles.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cycles", type=int, default=20)
    ap.add_argument("--pilot", action="store_true", help="2 cycles")
    ap.add_argument("--out", required=True)
    ap.add_argument("--kit", help="the comfy-agent folder (for `ops --json` and the commit)")
    ap.add_argument("--comfy", default=os.environ.get("COMFY_URL", "http://127.0.0.1:8188"))
    ap.add_argument("--llm", default=os.environ.get("LLM_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--pi", default=shutil.which("pi") or "pi")
    ap.add_argument("--provider", default="local", help="Pi provider id from models.json")
    ap.add_argument("--model", default="", help="Pi model id (and router model name)")
    ap.add_argument("--expect-provider", default="local")
    ap.add_argument("--llama-server", default=shutil.which("llama-server") or "")
    ap.add_argument("--model-file", default="")
    ap.add_argument("--context", type=int, default=32768)
    ap.add_argument("--recipe", default="zimage")
    ap.add_argument("--image", default="", help="the person's picture, for an edit recipe such as qwen-edit")
    ap.add_argument("--workdir", default=str(Path.home()))
    ap.add_argument("--turn-deadline", type=float, default=1800)
    ap.add_argument("--notes", default="")
    ap.add_argument("--skip-preflight", action="store_true")
    a = ap.parse_args()
    if a.pilot:
        a.cycles = 2
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    problems, st, states = preflight(a)
    if problems and not a.skip_preflight:
        for p in problems:
            print("NOT READY:", p)
        sys.exit(2)
    (out / "run.json").write_text(json.dumps(record_env(a, st, states), indent=1))
    sampler = subprocess.Popen([sys.executable, str(HERE / "vram_sampler.py"), "sample", "--out",
                                str(out / "vram.jsonl"), "--comfy", a.comfy, "--llm", a.llm])
    results = []
    try:
        time.sleep(2)
        for i in range(a.cycles):
            r = run_cycle(a, i, out)
            results.append(r)
            ph = (r.get("op") or {}).get("phases") if isinstance(r.get("op"), dict) else None
            print(f"cycle {i + 1}/{a.cycles}: {'pass' if r['pass'] else 'FAIL'} "
                  f"render turn {r['render_turn_s']} s, phases {ph}, "
                  f"{'' if r['pass'] else [k for k, v in r['checks'].items() if not v]}", flush=True)
            if not r["pass"] and a.cycles > 2 and i < 2:
                print("stopping early: a pilot cycle failed; fix before a long run")
                break
    finally:
        time.sleep(2)
        sampler.terminate()
        sampler.wait(10)
    vs = subprocess.run([sys.executable, str(HERE / "vram_sampler.py"), "summary", str(out / "vram.jsonl"),
                         "--markers", str(out / "markers.jsonl")], capture_output=True, text=True)
    try:
        vram = json.loads(vs.stdout)
    except ValueError:
        vram = {"_raw": vs.stdout[:500]}
    phases = [r["op"]["phases"] for r in results if isinstance(r.get("op"), dict) and r["op"].get("phases")]
    avg = {k: round(sum(p.get(k, 0) or 0 for p in phases) / len(phases), 1) for k in phases[0]} if phases else {}
    summary = {"cycles_run": len(results), "cycles_passed": sum(r["pass"] for r in results),
               "avg_phase_s": avg, "vram": vram,
               "pass": bool(results) and all(r["pass"] for r in results) and vram.get("violations") == 0}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    sys.exit(0 if summary["pass"] else 1)


if __name__ == "__main__":
    main()
