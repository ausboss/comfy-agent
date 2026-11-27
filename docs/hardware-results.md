# Hardware results: Pi and ComfyUI sharing one GPU

One row per machine and run. A result counts for that machine and setup only: one card passing says nothing
about others. Labels: **real** = ran on that hardware; **simulated** = fake servers or a big card held down to
act like a small one. How to run: [examples/pi-local/README.md](../examples/pi-local/README.md). Pinned
versions: [examples/pi-local/versions.json](../examples/pi-local/versions.json).

## Status (2026-11-27)

| Machine | GPU | Router probe | Pilot (2 cycles) | 20 cycles | Timeout, crash, memory checks |
|---|---|---|---|---|---|
| arch3090 | RTX 3090 24 GB, Ampere | not run | not run | not run | not run |
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

## Real runs

None yet. Each run records: OS, driver, ComfyUI version, Pi, llama.cpp build, model file and quant, context,
recipe, kit commit, peak VRAM per side, co-residency violations, and time to unload, render, free and reload
(from `ops --json`).
