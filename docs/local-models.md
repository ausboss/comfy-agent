# A local model on a small card

Your agent's model and ComfyUI both want the graphics card. On a 32 GB card, Qwen3.8 27B at 3-bit (about 19 GB
with a 131K context) and most image workflows fit side by side. On a 16 GB card they don't, so they take turns:

1. The agent decides to make something and calls the kit (`run`, or `generate` through MCP).
2. The kit runs your unload command, and the model leaves the card.
3. ComfyUI renders with the whole card. The kit waits for every result.
4. The kit tells ComfyUI to unload its models.
5. The tool call returns. The agent's next request makes your model server load the model back, and the agent
   carries on: it looks at the pictures, picks one, asks for the next thing.

Each turn costs a model load and the prompt cache: the next request reads the whole conversation again. After
the first load the model file sits in the system's file cache, so loading again is much faster than the
first time.

## Set it up

You need a model server that loads a model by itself when a request comes in. llama-swap, LM Studio (with
just-in-time loading on) and Ollama all do.

### llama-swap (tested)

[llama-swap](https://github.com/mostlygeek/llama-swap) starts llama-server when a request names a model, and
`GET /unload` stops it.

```bash
python3 comfy_agent.py config unload_llm="curl -sf http://127.0.0.1:8080/unload"
```

If your llama-swap has an API key: `unload_llm="curl -sf -H 'Authorization: Bearer YOUR-KEY' http://127.0.0.1:8080/unload"`.

A Qwen3.8 27B entry for a 16 GB card. The 3-bit file is 13.4 GB and its vision part 0.9 GB, so the context
has to be smaller than on a 32 GB card:

```yaml
models:
  qwen38-16g:
    cmd: >
      llama-server --host 127.0.0.1 --port ${PORT}
      -ngl 999 -fa on -np 1 --no-context-shift
      --cache-type-k q4_0 --cache-type-v q4_0 -b 2048 -ub 512
      --spec-type draft-mtp --spec-draft-n-max 2 --reasoning-format deepseek
      -m /path/to/Qwen3.8-27B-UD-Q3_K_XL.gguf --mmproj /path/to/mmproj-BF16.gguf
      -c 32768
```

### LM Studio (not tested here)

```bash
python3 comfy_agent.py config unload_llm="lms unload --all"
```

Turn on just-in-time model loading in LM Studio's server settings, so the next request loads the model again.

### Ollama (not tested here)

```bash
python3 comfy_agent.py config unload_llm="curl -sf http://127.0.0.1:11434/api/generate -d '{\"model\": \"qwen3.8:27b\", \"keep_alive\": 0}'"
```

Use the model name you run. Ollama loads it again on the next request.

### Turning it off

```bash
python3 comfy_agent.py config unload_llm=
```

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

## Things to know

- In this mode `run` always waits for its results, even with `--no-wait`: if the agent took another turn
  while ComfyUI was still rendering, the model would load back into a full card. For long videos, raise
  `--timeout` (seconds, default 600).
- The MCP `generate` tool waits up to 30 minutes in this mode instead of 4.
- There's nothing to unload with a cloud model (Claude, Codex, ChatGPT), so leave `unload_llm` empty for those.
