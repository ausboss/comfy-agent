# Pi with a local model on the same GPU as ComfyUI

Milestone 1 setup and test tools: Pi talks to a llama.cpp router on this computer, and the kit's MCP server
moves the model off the GPU while ComfyUI renders, then puts it back. The lifecycle itself is the core's job
([docs/lifecycle.md](../../docs/lifecycle.md)); this folder is the Pi side and the evidence tools.
Results per machine go in [docs/hardware-results.md](../../docs/hardware-results.md).

## Pinned versions

[versions.json](versions.json): llama.cpp `b12808`, Pi `1.6.1` (`@earendil-works/pi-coding-agent`), model
`unsloth/Qwen3.8-27B-GGUF` file `Qwen3.8-27B-UD-Q3_K_XL.gguf` (13.1 GB, sha256 in the file) at 32K context.

## Set up

1. Get llama.cpp at the pinned tag. On Linux with NVIDIA, the release's prebuilt CUDA 12.8 build (it ships its
   CUDA runtime libraries) worked on arch3090 without a CUDA toolkit. Or build it (CUDA example; use the matching
   backend on other GPUs):
   `git clone --branch b12808 --depth 1 https://github.com/ggml-org/llama.cpp && cmake -S llama.cpp -B llama.cpp/build -DGGML_CUDA=ON && cmake --build llama.cpp/build -j --target llama-server`
2. Put the model in `~/models/pi-local/Qwen3.8-27B-UD-Q3_K_XL/` (the `.gguf` and `mmproj-F16.gguf`).
3. Start the router: `./start-router.sh`, then load the model once:
   `curl -X POST http://127.0.0.1:8080/models/load -H 'Content-Type: application/json' -d '{"model":"qwen38-27b"}'`
4. Pi: `npm i -g @earendil-works/pi-coding-agent@1.6.1`, then copy [pi/models.json](pi/models.json),
   [pi/mcp.json](pi/mcp.json) and [pi/settings.json](pi/settings.json) into `~/.pi/agent/` (fix the path to
   `comfy_agent.py` in `mcp.json`).
5. The kit: `python3 comfy_agent.py config inference.type=llama-router inference.url=http://127.0.0.1:8080 inference.model=qwen38-27b`

Why a static `models.json` provider instead of Pi's built-in llama.cpp provider: the built-in one lists only
models that are loaded (with autoload off), and the kit unloads the model during every render. A fixed entry
keeps the session on the local model; the request fails with "model is not loaded" instead of Pi picking
something else.

## What Pi does with a long tool call and an unloaded model (probed)

Real Pi 1.6.1 against fake backends ([probes/probe_pi.py](probes/probe_pi.py); no GPU, simulated server):

| Case | What happened |
|---|---|
| Tool call longer than the MCP `timeout` (default 60 s per server) | At the timeout Pi sends `notifications/cancelled`, keeps the server running, gives the model the tool error `MCP request timed out after Nms`, and calls the model again at once |
| Same, server sends `notifications/progress` | Each progress resets the timer: a 30 s call finished with a 10 s timeout and progress every 5 s. Pi always sends a `progressToken` |
| Model call while the router has the model unloaded | HTTP 400 `model is not loaded`. Pi does not retry it; the turn ends with that error. The provider stays local, no fallback |
| 503 `Loading model` | Retried automatically (3 tries, 2 s backoff), then answered |
| Second tool call after a timeout, sequential stdio server | Also timed out, queued behind the first. The core's parallel stdio server is meant to fix this |

So with the router, Pi can't read a tool result while the model is unloaded. The MCP `generate` call has to
stay open (with progress) until the model is back; if it returns early, Pi's turn ends with the 400 and the
person has to prompt again once the render is done. `timeout: 120` in `mcp.json` is a safety margin over the
kit's 10 s progress interval, not a render limit.

Router details read from the `b12808` source (`tools/server/server-models.cpp`): with `--no-models-autoload`
a request for an unloaded model is refused, but a request carrying `?autoload=true` loads it anyway, and a
sleeping model wakes on any request. [start-router.sh](start-router.sh) therefore leaves sleep off.

## Test tools

| File | Does |
|---|---|
| [probes/probe_pi.py](probes/probe_pi.py) | Pi behaviour above, with [fake_llm.py](probes/fake_llm.py) and [slow_mcp.py](probes/slow_mcp.py). Cloud-safe |
| [probes/probe_router.py](probes/probe_router.py) | On a real router: refusal while unloaded, then load/unload rounds with timings and free memory. Uses the GPU |
| [vram_sampler.py](vram_sampler.py) | Samples every 0.5 s: ComfyUI `/system_stats` and `/queue`, router `/models`, and `nvidia-smi` per process when present. `summary` checks that the model is never resident while ComfyUI is executing |
| [cycle_driver.py](cycle_driver.py) | N cycles of Pi in RPC mode: render a picture, then ask for its seed. Checks each cycle, records versions, `ops --json` timings and the sampler summary. `--pilot` = 2 cycles |

Run order on a machine (one heavy GPU job at a time; check `nvidia-smi` and ComfyUI's queue first):
`probe_router.py --rounds 3` → `cycle_driver.py --pilot` → look at the pictures → `cycle_driver.py --cycles 20`.
