# GPU handoff: the lifecycle contract

This is the contract between the kit's core (`comfy_agent.py`, `comfy_lifecycle.py`) and anything that drives
or checks it: Pi, the GPU test driver in `examples/pi-local/`, other agents, and people. If you change a state
name, a config key or an output line below, change this file in the same commit.

Status: milestone 1. Linux is the target; macOS and Windows should work but are untested. Nothing here
assumes a particular GPU: free memory comes from ComfyUI's own `/system_stats`.

## Two modes

| Mode | When | What happens |
|---|---|---|
| unmanaged | no `inference` config and no `unload_llm` | As before: jobs are submitted and waited for in the calling process. Every accepted job is still recorded in the journal the moment ComfyUI accepts it. No lease, no runner. |
| managed | `inference.type` is `command`, `llama-swap` or `llama-router` (or the legacy `unload_llm` is set, which means `command`) | The sequence below runs in a detached runner process that holds the GPU lease. The CLI or MCP call only follows the journal. |

## Config

In `comfy-agent.local.json` (or the `COMFY_AGENT_INFERENCE` environment variable as JSON). Set keys with
`python3 comfy_agent.py config inference.type=llama-router inference.url=http://127.0.0.1:8080`.

| Key | Default | Meaning |
|---|---|---|
| `inference.type` | `none` | `none`, `command`, `llama-swap`, `llama-router` |
| `inference.url` | `http://127.0.0.1:8080` | the model server (llama-swap or llama-server in router mode) |
| `inference.model` | (all loaded) | only unload and restore this model. Without it: whatever was loaded before the unload |
| `inference.api_key` / `inference.api_key_env` | none | sent as `Authorization: Bearer` |
| `inference.unload` / `inference.load` / `inference.ready` | none | shell commands, for `command`. `ready` exits 0 when the model is back |
| `inference.need_mb` | measured | free MiB the model needs to load. Default: what the unload freed, as ComfyUI saw it |
| `inference.margin_mb` | `512` | added to `need_mb` |
| `inference.unload_timeout` | `120` | seconds for the model to leave the GPU |
| `inference.free_timeout` | `180` | seconds after `/free` for the memory to come back |
| `inference.idle_timeout` | `3600` | seconds to wait for ComfyUI's queue to empty (browser jobs) before the reload |
| `inference.load_timeout` | `300` | seconds for the model to report ready |
| `inference.device` | `0` | index into `/system_stats` `devices` |
| `inference.lease_dir` | per computer (below) | folder for the GPU lease. Also `COMFY_AGENT_LEASE_DIR` |

`unload_llm=COMMAND` keeps working and means `{"type": "command", "unload": COMMAND}`.

Recommended for Pi on a shared card: llama.cpp `llama-server` in router mode started with
`--no-models-autoload`, and `inference.type=llama-router`. The server then refuses to load a model by itself,
so a stray request during a render gets an error instead of loading into a full card. With llama-swap the kit
uses `POST /api/models/pause` and `/resume` when the build has them (they reject requests while paused), and
falls back to a plain unload, where only convention stops a reload. The router honours a per-request
`?autoload=true` even with `--no-models-autoload`; pi's built-in llama.cpp provider sends `autoload=false`.
To cover those gaps the runner checks the model server every 2 s while it holds the lease (render, idle wait,
free). If a model came back, it unloads it again and records an `intrusion` note. That shortens an overlap; it
can't prevent one.

## The managed sequence

1. **Validate** (calling process): recipe, settings, missing nodes and models, uploads of the person's files,
   and every prompt built. Nothing is unloaded if this fails.
2. **Record** the operation and its jobs (state `planned`) in the journal. Start the runner.
3. **Lease**: the runner takes the GPU lease. Other operations wait here (`queued`).
4. **Unload** through the adapter. Confirm: the adapter's status says unloaded. Record free memory before
   and after; the difference is the default `need_mb`.
5. **Submit** each job. The prompt id is minted by the kit and stored before the POST, so a dropped
   connection leaves an `uncertain` job that is looked up in ComfyUI's queue and history by that id. It is
   never sent twice. A refused job stops the batch; jobs already accepted still render and are collected.
6. **Render**: collect each job's files as it finishes. `--send` and the gallery page run here; their failure
   is a note, never a failed render.
7. **Idle**: wait until ComfyUI's queue is empty, browser jobs included.
8. **Free**: `POST /free`, then poll `/system_stats` until `vram_free - torch_vram_free` on the device is at
   least `need_mb + margin_mb`. If it isn't by `free_timeout`, the operation ends `blocked` and the model is
   **not** loaded.
9. **Restore**: load what was loaded before step 4 and wait until the adapter says ready.
10. Release the lease.

Invariants the tests check: no prompt is submitted twice; the adapter's load is never called while ComfyUI
reports a running or pending job; one unload and at most one load per lease.

## Operation states

`queued` → `unloading` → `submitting` → `rendering` → `idle-wait` → `freeing` → `restoring` → one of
(a `blocked` operation that `reconcile` retries goes back to `queued`):

| Final state | Meaning |
|---|---|
| `done` | every step ran. Individual jobs may still have failed (see the jobs) |
| `failed` | nothing could be rendered (for example the model wouldn't unload), and the GPU was left as it was found |
| `cancelled` | cancelled by `cancel`; owned jobs removed from ComfyUI, then the model restored |
| `blocked` | the model is still unloaded: memory didn't come back, ComfyUI stayed busy or stopped answering, or the load failed. `error` says which. The next operation, or `reconcile`, restores it |

Unmanaged operations go straight from `submitting` to `rendering` to `done`.

## Job states

| State | Meaning |
|---|---|
| `planned` | built, not sent |
| `submitting` | the POST is in flight (seen only after a crash) |
| `uncertain` | the POST's answer was lost; being matched against ComfyUI's queue and history |
| `queued` | ComfyUI accepted it; `prompt_id` is set |
| `done` | files saved (`files`) |
| `failed` | ComfyUI refused it or the run errored (`error`) |
| `lost` | accepted, but ComfyUI no longer knows it (restarted) |
| `skipped` | not sent, because an earlier job in the batch was refused or the operation was cancelled |
| `cancelled` | removed from ComfyUI's queue or interrupted |

A job's files are returned to the caller once: `delivered` is set when a `run`, `wait`, `generate` or
`results` reports them.

## Files

All in the output folder (`out`, default `outputs/`):

- `.comfy-agent.db`: the journal (SQLite, WAL). Tables `ops` and `jobs`; read it with `ops --json`, not SQL.
- `.runs/OP.log`: the runner's output for that operation.

The older `.comfy-agent-jobs.json` is read once and its pending jobs moved into the journal.

The GPU lease is **one per computer**, not per output folder, so two copies of the kit (or two output folders)
on one card still take turns. It lives in `$XDG_STATE_HOME/comfy-agent/` (default `~/.local/state/comfy-agent/`)
on Linux, `~/Library/Application Support/comfy-agent/` on macOS and `%LOCALAPPDATA%\comfy-agent\` on Windows:

- `.gpu.lock`: the lease, an OS file lock (`flock`, or `msvcrt.locking` on Windows). The OS drops it when the
  holder dies.
- `.gpu.lease.json`: who holds it (`op`, `out`, `pid`, `host`, `since`).

Set `inference.lease_dir` to give each card its own lease on a computer with several. Keep the lease and the
journal on a local disk: file locks and SQLite are unreliable on network shares. A blocked operation is only
restored by a later operation from the same output folder (or by `reconcile` there).

## Commands

| Command | Does |
|---|---|
| `run`, `wait` | unchanged flags. In managed mode they follow the runner and print the lines below. `run --no-wait` returns at once only with `llama-router` (the server can't reload by itself); with other adapters it waits anyway |
| `ops [OP] [--json]` | recent operations: state, error, per-phase timings and free memory |
| `cancel [OP]` | cancel one operation, or every unfinished one |
| `reconcile` | restart a runner for any unfinished operation whose runner died, and retry `blocked` ones |

`run`, `wait`, `doctor`, `ops` and the MCP server reconcile dead runners on start (not `blocked` ones; that
needs `reconcile`, because blocked usually means something else is holding memory).

## Output lines

Stable prefixes, one line each, so drivers can grep them:

```text
queued 2 job(s) of zimage as operation 20261127-052233-ab12
waiting for the GPU: operation 20261127-052201-9f3c has it
unloaded the language model (qwen38-16g), 14.9 GB freed
done 1/2, seed 123, 3.1 s: outputs/zimage/20261127-052233-01.png
FAILED 2/2: KSampler (node 7): ...
restored the language model (qwen38-16g)
still rendering: operation 20261127-052233-ab12, 1 of 2 done. Collect with: python3 comfy_agent.py wait
BLOCKED: memory not released (9.1 GB free, need 15.4 GB). The language model stays unloaded. ...
```

## MCP

Still three tools. In managed mode `generate` follows the operation until it ends, which is after the model is
back: a client whose own model is unloaded can't read an early answer (with the llama.cpp router, pi's next
model call gets `400 model is not loaded` and the turn ends). It returns early only when the client cancels the
request or after `COMFY_AGENT_MCP_WAIT` seconds (default 10,800), with the `still rendering` line and the
operation id. In unmanaged mode it waits 240 s, as before.

If the request carries a `progressToken`, the server sends `notifications/progress` every 10 s. pi 1.6.1 sends a
token on every call and resets its 60 s per-request timeout on each notification (checked by the GPU thread
with a fake server), so long renders don't time out there.

`results` takes two optional arguments: `operation` (follow just that one) and `cancel: true`. It returns each
file once. The stdio server answers requests in parallel and honours `notifications/cancelled` (it stops
following and sends no answer for that request; the operation goes on), so a client that gave up on
`generate` can call `results` straight away.

## `ops --json`

One object per operation:

```json
{"id": "20261127-052233-ab12", "state": "done", "mode": "managed", "recipe": "zimage", "error": null,
 "adapter": "llama-router", "models": ["qwen38-16g"], "jobs": [{"n": 1, "state": "done", "prompt_id": "…",
 "seed": 123, "files": ["…"]}], "phases": {"lease": 0.0, "unload": 4.2, "render": 6.3, "idle": 0.0,
 "free": 1.1, "restore": 9.8}, "vram_mb": {"before_unload": 8100, "after_unload": 23050,
 "after_free": 22900}, "notes": []}
```

`phases` are seconds spent in each step. `vram_mb` is free device memory as ComfyUI reports it (`null` when
ComfyUI has no figure: CPU, DirectML). `notes` holds `intrusion: ...` when the guard above fired, and
`no free-memory check: ...` when the headroom check couldn't run.

## Platforms

The runner is a detached process: a new session on Linux and macOS; on Windows its own process group with no
console, and out of the client's job object when the job allows it, so a client that kills its server's
process tree doesn't kill the render. CI runs the tests on Linux (Python 3.9 and 3.13), macOS and Windows,
against fake servers. Real-GPU results are per machine in `docs/hardware-results.md`.

## Not covered in milestone 1

The browser is detected, not blocked: a browser job that is running delays the reload, and a browser job
queued in the middle of a render is not prevented. Separate GPUs for the model and ComfyUI, and keeping both
resident, are out of scope.
