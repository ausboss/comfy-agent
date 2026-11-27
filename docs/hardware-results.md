# Hardware results: Pi and ComfyUI sharing one GPU

One row per machine and run. A result counts for that machine and setup only: one card passing says nothing
about others. Labels: **real** = ran on that hardware; **simulated** = fake servers or a big card held down to
act like a small one. How to run: [examples/pi-local/README.md](../examples/pi-local/README.md). Pinned
versions: [examples/pi-local/versions.json](../examples/pi-local/versions.json).

## Status (2026-11-27)

| Machine | GPU | Router probe | Pilot (2 cycles) | 20 cycles | Timeout, crash, memory checks |
|---|---|---|---|---|---|
| arch3090 | RTX 3090 24 GB, Ampere | blocked: no cmake or CUDA toolkit (2026-11-27) | not run | not run | not run |
| arch5090 | RTX 5090 32 GB, Blackwell | not run | not run | not run | not run |
| Legion | RTX 3070 Laptop 8 GB | M2 | M2 | M2 | M2 |
| metro-a5000 | RTX A5000 24 GB, 15 GB RAM | M2 | M2 | M2 | M2 |
| AMD, Intel, Apple, Windows | none available | | | | |

## Simulated checks (no GPU)

### Pi 1.6.1 MCP timeout and unloaded model, 2026-11-27, cloud container

Real Pi 1.6.1 (`@earendil-works/pi-coding-agent`), fake OpenAI-compatible server and fake slow MCP server
(`examples/pi-local/probes/probe_pi.py`). All six scenarios ran once.

| Scenario | Result |
|---|---|
| 30 s tool call, `timeout: 10` | Tool error `MCP request timed out after 10000ms` at 10.0 s; Pi sent `notifications/cancelled`; server kept running and sent its result at 30 s (ignored); next model call immediately |
| same, progress every 5 s | Completed at 30.0 s, no timeout |
| timeout, then model unloaded | Model call got 400 `model is not loaded`; no retry; turn ended with the error; provider stayed `local` |
| two calls after a timeout, sequential server | Both timed out at 10 s (the second was blocked behind the first) |
| 503 `Loading model` twice | Retried twice (2 s, 4 s backoff), then answered |
| 400 on the first call | No retry, turn ended |

### Pi 1.6.1 + the kit's handoff (PR #1 at db63579), whole fake stack, 2026-11-27, cloud container

Real Pi in RPC mode, the real kit from branch `gpu-handoff-lifecycle` as Pi's MCP server with
`inference.type=llama-router`, and the kit's own fakes (ComfyUI with memory books, router with autoload off)
plus a scripted chat model that answers 400 while unloaded (`examples/pi-local/probes/sim_stack.py`), driven by
`cycle_driver.py`.

| Run | Result |
|---|---|
| 3 cycles, 3 s fake render, `timeout: 120` | 3/3 pass: one generate each, file on disk, follow-up named the seed, every reply from `local/qwen`. Sampler: 0 violations (source: router status, no nvidia-smi) |
| 1 cycle, 75 s fake render, Pi `timeout: 60` | Pass. Render turn 76.8 s: the kit's 10 s progress kept Pi's call open past its 60 s timeout. No 400s reached Pi |

This checks the driver, the sampler and the Pi/kit wiring. It says nothing about real load, unload or free
timings, or real memory.

## Real runs

### arch3090, 2026-11-27 08:30 UTC: build blocked

Linux 7.2.15-4-omarchy (Arch), RTX 3090 24576 MiB, driver 615.78.08 (CUDA 13.4 user-mode driver), gcc 16.2.1,
Python 3.14.8. Before: 833 MiB used (idle test ComfyUI on :8199 at 256 MiB, plus the desktop), queue empty.
`first-check.sh` stopped at the llama.cpp build: no `cmake`, no `nvcc`. Nothing loaded on the GPU; 833 MiB
after. A user on a fresh Arch desktop needs `sudo pacman -S cmake cuda` before building, or a prebuilt
llama-server. The script now checks for both up front and says so.

None of the measured runs below exist yet. Each run records: OS, driver, ComfyUI version, Pi, llama.cpp build, model file and quant, context,
recipe, kit commit, peak VRAM per side, co-residency violations, and time to unload, render, free and reload
(from `ops --json`).
