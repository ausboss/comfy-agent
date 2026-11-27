#!/usr/bin/env python3
"""Sample GPU memory, the model server and ComfyUI twice a second, and check the handoff invariant.

  python3 vram_sampler.py sample --out run/vram.jsonl [--comfy URL] [--llm URL] [--interval 0.5]
  python3 vram_sampler.py summary run/vram.jsonl [--markers run/markers.jsonl]

Nothing here assumes a particular card. Each sample records what it could read:
  - ComfyUI /system_stats (any GPU ComfyUI supports): device total, free, torch reserved
  - ComfyUI /queue: is a job running or pending
  - the model server: llama.cpp router GET /models statuses, or llama-swap GET /running
  - nvidia-smi when present: memory used per process, split into "llm" (llama-server) and "comfy" (the
    process ComfyUI runs in), so co-residency is measured, not guessed. Without nvidia-smi (AMD, Intel,
    Apple) the per-process split is missing and the check falls back to the model server's status.

The invariant (plan A1): the language model is never resident while ComfyUI is executing.
A sample violates it when ComfyUI reports a running job and either the llm process holds more than
--llm-threshold MiB, or (no per-process data) the model server reports a model loaded or loading.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def get_json(url, timeout=1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except Exception:
        return None


def proc_role(pid, comfy_pids):
    if pid in comfy_pids:
        return "comfy"
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return "other"
    if "llama-server" in cmd or "llama serve" in cmd or "llama-swap" in cmd:
        return "llm"
    if "ComfyUI" in cmd or "comfy" in cmd.lower() and "main.py" in cmd:
        return "comfy"
    return "other"


def nvidia(device, comfy_pids):
    if not shutil.which("nvidia-smi"):
        return None
    try:
        g = subprocess.run(["nvidia-smi", "-i", str(device), "--query-gpu=memory.used,memory.total,uuid",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3).stdout
        used, total, uuid = [x.strip() for x in g.strip().split(",")]
        apps = subprocess.run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_memory",
                               "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    split = {"llm": 0, "comfy": 0, "other": 0}
    procs = []
    for line in apps.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) != 3 or parts[0] != uuid or not parts[1].isdigit():
            continue
        pid, mb = int(parts[1]), int(parts[2]) if parts[2].isdigit() else 0
        role = proc_role(pid, comfy_pids)
        split[role] += mb
        procs.append([pid, role, mb])
    return {"used_mb": int(used), "total_mb": int(total), **{f"{k}_mb": v for k, v in split.items()},
            "procs": procs}


def comfy_stats(url, device):
    st = get_json(url + "/system_stats")
    q = get_json(url + "/queue")
    out = {}
    if st and st.get("devices"):
        d = st["devices"][min(device, len(st["devices"]) - 1)]
        mb = 1024 * 1024
        out.update({"comfy_total_mb": d.get("vram_total", 0) // mb, "comfy_free_mb": d.get("vram_free", 0) // mb,
                    "comfy_torch_mb": d.get("torch_vram_total", 0) // mb,
                    "comfy_torch_free_mb": d.get("torch_vram_free", 0) // mb, "device_name": d.get("name")})
        out["comfy_pid"] = (st.get("system") or {}).get("pid")
    if q is not None:
        out["running"] = len(q.get("queue_running", []))
        out["pending"] = len(q.get("queue_pending", []))
    out["comfy_up"] = st is not None
    return out


def llm_status(url):
    m = get_json(url + "/models")
    if m and isinstance(m.get("data"), list) and any("status" in x for x in m["data"]):
        return {"llm_kind": "llama-router",
                "llm_models": {x.get("id"): (x.get("status") or {}).get("value") for x in m["data"]}}
    r = get_json(url + "/running")
    if r is not None:
        return {"llm_kind": "llama-swap",
                "llm_models": {x.get("model"): x.get("state", "ready") for x in (r.get("running") or [])}}
    return {"llm_kind": None, "llm_models": {}}


def cmd_sample(a):
    out = open(a.out, "a", buffering=1)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    comfy_pids = set(int(x) for x in a.comfy_pid.split(",") if x) if a.comfy_pid else set()
    t_end = time.time() + a.duration if a.duration else None
    while not stop and (t_end is None or time.time() < t_end):
        t = time.time()
        rec = {"t": round(t, 3)}
        c = comfy_stats(a.comfy, a.device)
        if c.get("comfy_pid"):
            comfy_pids.add(int(c["comfy_pid"]))
        rec.update(c)
        if a.llm:
            rec.update(llm_status(a.llm))
        n = nvidia(a.device, comfy_pids)
        if n:
            rec["nv"] = n
        out.write(json.dumps(rec) + "\n")
        time.sleep(max(0, a.interval - (time.time() - t)))


def resident(rec, threshold):
    """Is the language model on the card in this sample? (True/False, how we know)"""
    nv = rec.get("nv")
    if nv and nv.get("procs"):
        return nv["llm_mb"] > threshold, "nvidia-smi per process"
    states = set((rec.get("llm_models") or {}).values())
    if rec.get("llm_kind"):
        return bool(states & {"loaded", "loading", "ready", "starting"}), "model server status"
    return None, "unknown"


def cmd_summary(a):
    recs = [json.loads(x) for x in open(a.file) if x.strip()]
    if not recs:
        print("no samples")
        return 1
    marks = [json.loads(x) for x in open(a.markers)] if a.markers and os.path.exists(a.markers) else []
    viol, how = [], set()
    peak = {"used": 0, "llm": 0, "comfy": 0, "both_when_running": 0}
    gaps = 0
    for prev, rec in zip([None] + recs[:-1], recs):
        if prev and rec["t"] - prev["t"] > 3 * a.interval:
            gaps += 1
        res, src = resident(rec, a.llm_threshold)
        how.add(src)
        nv = rec.get("nv") or {}
        peak["used"] = max(peak["used"], nv.get("used_mb", 0) or (rec.get("comfy_total_mb", 0) - rec.get("comfy_free_mb", 0)))
        peak["llm"] = max(peak["llm"], nv.get("llm_mb", 0))
        peak["comfy"] = max(peak["comfy"], nv.get("comfy_mb", 0))
        if rec.get("running"):
            peak["both_when_running"] = max(peak["both_when_running"], nv.get("llm_mb", 0) + nv.get("comfy_mb", 0))
            if res:
                viol.append(rec)
    # end-of-cycle free memory: the sample nearest each "cycle_end" marker
    ends = []
    for m in marks:
        if m.get("event") == "cycle_end":
            near = min(recs, key=lambda r: abs(r["t"] - m["t"]))
            ends.append({"cycle": m.get("cycle"), "free_mb": near.get("comfy_free_mb"),
                         "used_mb": (near.get("nv") or {}).get("used_mb")})
    trend = None
    pts = [(i, e["used_mb"] if e["used_mb"] is not None else -(e["free_mb"] or 0)) for i, e in enumerate(ends)]
    if len(pts) >= 3:
        n = len(pts)
        mx, my = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
        den = sum((p[0] - mx) ** 2 for p in pts) or 1
        trend = round(sum((p[0] - mx) * (p[1] - my) for p in pts) / den, 1)  # MiB per cycle, + = leaking
    s = {"samples": len(recs), "seconds": round(recs[-1]["t"] - recs[0]["t"], 1), "sample_gaps": gaps,
         "residency_source": sorted(how), "violations": len(viol),
         "first_violation_t": viol[0]["t"] if viol else None,
         "peak_used_mb": peak["used"], "peak_llm_mb": peak["llm"], "peak_comfy_mb": peak["comfy"],
         "peak_llm_plus_comfy_while_rendering_mb": peak["both_when_running"],
         "end_of_cycle": ends, "end_of_cycle_used_trend_mb_per_cycle": trend,
         "device": next((r.get("device_name") for r in recs if r.get("device_name")), None),
         "total_mb": next(((r.get("nv") or {}).get("total_mb") or r.get("comfy_total_mb") for r in recs), None)}
    print(json.dumps(s, indent=1))
    return 1 if viol else 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--out", required=True)
    p.add_argument("--comfy", default=os.environ.get("COMFY_URL", "http://127.0.0.1:8188"))
    p.add_argument("--llm", default=os.environ.get("LLM_URL", "http://127.0.0.1:8080"))
    p.add_argument("--comfy-pid", default="", help="ComfyUI's pid(s), if /system_stats doesn't report it")
    p.add_argument("--device", type=int, default=0)
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--duration", type=float, default=0, help="stop after this many seconds (0 = until killed)")
    p = sub.add_parser("summary")
    p.add_argument("file")
    p.add_argument("--markers")
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--llm-threshold", type=int, default=1024, help="MiB above which the llm counts as resident")
    a = ap.parse_args()
    sys.exit(cmd_sample(a) if a.cmd == "sample" else cmd_summary(a))


if __name__ == "__main__":
    main()
