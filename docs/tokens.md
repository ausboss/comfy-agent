# What it costs an agent: measured

Every token a model reads is paid for, in money with a cloud model and in time with a local one, and it's paid
again on every turn. So the question isn't "MCP or not": it's how much the agent has to read to do the job.

## The same job four ways, with a local model

One task, word for word: *"Make me 3 pictures of a red fox curled up asleep in a snowy forest at dawn with my
ComfyUI, then tell me where the files are."* (The last three routes were also told the workflow file and
ComfyUI's address, since they have no recipe to find.)

Agent: [pi](https://github.com/earendil-works/pi) 1.6.0 with a clean profile (no skills, no extensions), thinking
low. Model: Qwen3.8 27B (UD-Q3_K_XL, 131K context) on llama.cpp, on the same RTX 5090 as ComfyUI 0.45.3.
Workflow: ComfyUI's Z-Image Turbo template. November 24, 2026. Token counts are Qwen's own, from pi's session log.

| Route | Time | Model calls | Tool calls | Input tokens, all calls | Biggest single prompt | Output tokens | Result |
|---|---:|---:|---:|---:|---:|---:|---|
| This kit's CLI + SKILL.md | 36 s | 7 | 8 | 28,266 | 7,648 | 2,388 | 3 pictures, then opened all 3 to check them |
| This kit's 3-tool MCP server | **17 s** | **4** | **3** | **11,067** | **4,851** | **662** | 3 pictures |
| Comfy's official comfy-mcp 0.11.0 | 166 s | 23 | 38 | 1,016,227 | 55,211 | 11,062 | 3 pictures, after fixing a model path |
| artokun comfyui-mcp, `--compact` | 174 s | 15 | 26 | 389,375 | 34,847 | 8,289 | 3 pictures, after converting the workflow and fixing a model path |
| Raw HTTP: the agent writes its own script | 107 s | 13 | 19 | 269,358 | 29,324 | 8,483 | 7 pictures (asked for 3), after reading the workflow and node schemas |

Things to know before quoting these:

- **One run each.** Treat them as a demonstration, not a benchmark. The CLI run, repeated later on the final
  SKILL.md, took 35 s, 8 model calls and 39,742 input tokens (biggest prompt 8,622): the same route can vary
  by a third from run to run.
- The CLI run includes the agent opening its three pictures to check them, which the MCP run skipped. Those
  image reads are most of the difference between the first two rows.
- "Input tokens, all calls" counts what every call read, including what the server's prompt cache already
  had. With a local model a cache hit is fast, so the biggest single prompt matters more for speed.
- The template names its model file at the top of the models folder; on this machine it sits in a subfolder.
  Both general MCP servers had to find and fix that. The kit matches model files by name in any subfolder.
- Most of the input on the long runs was already in the server's prompt cache, so the local model wasn't
  slowed by all of it. It still has to fit: the official server's run peaked at 55,211 tokens in one prompt.
- The raw route worked because this template is small (9 nodes). The AusBoss workflows are 25 to 36 nodes in
  subgraphs, and their files are 4 to 5 times bigger.
- Both the CLI and raw runs had the model look at its pictures, and both times Qwen reported a "two-headed fox"
  in a picture that has one fox. The raw run threw that picture away and made more. A small local model's
  eyes aren't reliable enough yet to judge results on their own.

## What each thing costs to read

Counted with tiktoken `o200k_base` (a stand-in; Qwen's tokenizer gives similar but not equal numbers).

| What the agent reads | Tokens |
|---|---:|
| artokun comfyui-mcp tool list, default mode (41 tools), sent with every call | 50,247 |
| artokun comfyui-mcp tool list, `--compact` (3 discovery tools) | 382, plus about 1,000 to 1,900 per tool it looks up |
| Comfy's official comfy-mcp 0.11.0 tool list (41 tools), sent with every call | 16,577 |
| ComfyUI's whole `/object_info` (3,360 node types on this machine) | 4,067,676 |
| One node type's `/object_info` (KSampler) | 1,091 |
| Klein 9B Outpaint (AusBoss), the saved workflow file | 16,909 |
| The same workflow in API format | 3,782 |
| comfy-cli's `workflow slots` for it (80 slots, with every model list) | about 15,000 |
| This kit's `SKILL.md` | about 850 |
| `list` with four recipes | about 120 |
| `show` for one recipe | about 150 |
| This kit's three MCP tool definitions | about 300 |

## Why

- An agent that reads a workflow graph pays for every node, link and note in it, and then has to get the
  conversion right. A recipe keeps the graph on disk and gives the agent a short menu of settings.
- A big MCP server pays for its whole tool list on every call, used or not: 23 calls with a 16,577-token list
  is 381,000 tokens before the agent has read anything else. Compact modes defer that, but the
  agent then spends calls looking tools up, and it still has the whole graph to deal with.
- MCP itself is not the problem: the three-tool server above was the cheapest run. Small tools that do one job
  well are cheap in any protocol.

## Reproduce

The pi profile for the local model (`~/.pi-test/models.json`, used with `PI_CODING_AGENT_DIR=~/.pi-test`):

```json
{"providers": {"localqwen": {
  "baseUrl": "http://127.0.0.1:30001/v1", "api": "openai-completions", "apiKey": "!cat /path/to/api-key.txt",
  "models": [{"id": "qwen38-fast", "reasoning": true, "input": ["text", "image"], "contextWindow": 131072,
              "maxTokens": 16384, "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}
```

```bash
cd comfy-agent
PI_CODING_AGENT_DIR=~/.pi-test pi --model localqwen/qwen38-fast --thinking low --no-skills \
  --session-dir ./sessions -p "Make me 3 pictures of a red fox curled up asleep in a snowy forest at dawn with my ComfyUI, then tell me where the files are."
```

Each assistant message in the session file has `usage.input`, `usage.cacheRead` and `usage.output`.
