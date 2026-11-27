# A local model on a small card

Your agent's model and ComfyUI both want the graphics card. On a 32 GB card, Qwen3.8 27B at 3-bit (about 19 GB
with a 131K context) and most image workflows fit side by side. On a 16 GB card they don't, so they take turns.
The kit runs the handoff; the full contract is [lifecycle.md](lifecycle.md):

1. The agent asks for something (`run`, or `generate` through MCP). The kit checks the recipe and uploads your
   files first, so a mistake never leaves the model unloaded.
2. A small background process (the runner) takes the GPU lease, so two sessions never hand the card over at
   the same time, and asks your model server to unload. It checks the model really left.
3. ComfyUI renders with the whole card. Each job is written down the moment ComfyUI accepts it.
4. When ComfyUI's queue is empty (browser jobs too), the kit asks it to free its memory and waits until
   ComfyUI reports enough free memory for the model. It reads that from ComfyUI, so it works the same on any
   card ComfyUI supports.
5. The kit loads the model back, waits until it's ready, and then the tool call returns.

If the agent's tool call times out, or the agent is closed, the runner keeps going. `wait` (or the MCP
`results` tool) collects the files later. If the memory never comes back, the kit stops with `BLOCKED` and
leaves the model unloaded rather than loading it into a full card; `python3 comfy_agent.py reconcile` retries.

Each turn costs a model load and the prompt cache: the next request reads the whole conversation again. After
the first load the model file sits in the system's file cache, so loading again is much faster than the
first time.

## Set it up

### llama.cpp router (recommended)

Start `llama-server` without a model, in router mode, with autoload off. Then the server itself refuses to
load a model while ComfyUI has the card: a stray request gets an error instead of an out-of-memory crash.

```bash
llama-server --models-dir /path/to/gguf --no-models-autoload --host 127.0.0.1 --port 8080
python3 comfy_agent.py config inference.type=llama-router inference.url=http://127.0.0.1:8080
```

Add `inference.model=NAME` to hand over only that model. Without it the kit unloads whatever is loaded and
loads back exactly that. `python3 comfy_agent.py doctor` shows what the kit sees.

### llama-swap (tested)

[llama-swap](https://github.com/mostlygeek/llama-swap) starts llama-server when a request names a model.

```bash
python3 comfy_agent.py config inference.type=llama-swap inference.url=http://127.0.0.1:8080
```

With a llama-swap build that has `POST /api/models/pause`, the kit pauses it during the render (it rejects
requests until resumed). Older builds only unload, so a request in the middle of a render would load the model
again. If your llama-swap has an API key: `inference.api_key=YOUR-KEY` (or `inference.api_key_env=VARIABLE`).

A Qwen3.8 27B entry for a 16 GB card. The 3-bit file is 13.4 GB and its vision part 0.9 GB, so the context
has to be smaller than on a 32 GB card. Leave the MTP draft (`--spec-type draft-mtp`) off: it adds about
570 MiB. With this entry, nvidia-smi showed the model using 14,976 MiB:

```yaml
models:
  qwen38-16g:
    cmd: >
      llama-server --host 127.0.0.1 --port ${PORT}
      -ngl 999 -fa on -np 1 --no-context-shift
      --cache-type-k q4_0 --cache-type-v q4_0 -b 2048 -ub 512
      --reasoning-format deepseek
      -m /path/to/Qwen3.8-27B-UD-Q3_K_XL.gguf --mmproj /path/to/mmproj-BF16.gguf
      -c 32768
```

### Any other server, with commands (not tested here)

```bash
python3 comfy_agent.py config inference.type=command inference.unload="lms unload --all"
```

`inference.load` and `inference.ready` (exits 0 when the model is back) are optional. Without a load command
the server has to load the model by itself on the next request: LM Studio with just-in-time loading, or
Ollama (`inference.unload="curl -sf http://127.0.0.1:11434/api/generate -d '{\"model\": \"qwen3.8:27b\", \"keep_alive\": 0}'"`).
The older `unload_llm=COMMAND` setting still works and means the same as `inference.unload`.

### Turning it off

```bash
python3 comfy_agent.py config inference.type= unload_llm=
```

### Fine-tuning

`inference.need_mb` (default: what the unload freed), `inference.margin_mb` (512), `inference.free_timeout`
(180 s), `inference.load_timeout` (300 s), `inference.idle_timeout` (3600 s for browser jobs). See
[lifecycle.md](lifecycle.md).

## The agent: pi with the local model

[pi](https://github.com/earendil-works/pi) talks to any OpenAI-compatible server. `~/.pi/agent/models.json`:

```json
{"providers": {"local": {
  "baseUrl": "http://127.0.0.1:8080/v1", "api": "openai-completions", "apiKey": "none",
  "models": [{"id": "qwen38-16g", "reasoning": true, "input": ["text", "image"], "contextWindow": 32768,
              "maxTokens": 8192, "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}
```

Then either install the kit as a skill (see the README) or add its MCP server to `~/.pi/agent/mcp.json`
([chat-apps.md](chat-apps.md)). The MCP route used the fewest tokens in our tests ([tokens.md](tokens.md)).

## What we measured

pi with the entry above, the kit's MCP server and this request: "Make 2 pictures of a red fox asleep in the
snow at dawn with my ComfyUI. Then take the better one and make it night time with a full moon." The card was
a 32 GB RTX 5090 with ComfyUI started with `--reserve-vram 17.5`, so ComfyUI had about as much room as on a
16 GB card.

- It finished in 148 seconds with 9 model calls and 4 ComfyUI jobs.
- Sampled every half second, the model and a working ComfyUI were never on the card at the same time.
- The most the two used together was 15,976 MiB: the model peaked at 15,126 MiB and ComfyUI at 15,296 MiB.

This was a big card made to act like a small one (`--reserve-vram` is not a hard cap, so treat it as a
simulation), measured with the older `unload_llm` handoff, before the runner and the free-memory check. Your
desktop uses some of a real 16 GB card too. If the
model fails to load after a picture, lower `-c` to 16384, which saved about 450 MiB in our tests.

## Things to know

- `python3 comfy_agent.py ops` lists recent handoffs with their state; `ops OP --json` adds the time each
  step took and the free memory ComfyUI reported. `cancel` stops one; `reconcile` finishes any whose runner died
  and retries blocked ones.
- In this mode `run` waits for its results even with `--no-wait`, except with the llama.cpp router, where the
  server can't reload by itself. For long videos, raise `--timeout` (seconds, default 600); the render goes on
  either way.
- The MCP `generate` tool follows the job until the model is back, and sends a progress notification every
  10 seconds so clients such as pi don't time out. `COMFY_AGENT_MCP_WAIT` (seconds) cuts it short.
- Qwen3.8 27B sometimes sees problems that aren't there. In two of our runs it said a sleeping fox had two
  heads and made more pictures; every fox had one head. If it starts redoing work, ask it to show you the
  pictures and let you judge.
- There's nothing to unload with a cloud model (Claude, Codex, ChatGPT), so leave `inference` unset for those.
- The GPU lease is one per computer, so two copies of the kit on one card take turns. With several cards, give
  each its own: `inference.lease_dir=/some/folder` ([lifecycle.md](lifecycle.md)).
- If a request loads the model back in the middle of a render (an older llama-swap, or a client that asks the
  router for `autoload=true`), the kit unloads it again within about 2 seconds and notes it in `ops`.
