# What we found, and why this kit is built the way it is

Research and tests from November 24, 2026, on one RTX 5090 with ComfyUI 0.45.3. Two research passes (a survey of
every ComfyUI agent project we could find, and token measurements) plus our own runs with Claude, and with
[pi](https://github.com/earendil-works/pi) on a local Qwen3.8 27B. The run-by-run numbers are in
[tokens.md](tokens.md).

## Give the agent a menu, not the workflow

A saved ComfyUI workflow is written for the editor: every node's position, colour, links, notes and model lists.
AusBoss's Klein 9B Outpaint is 16,909 tokens as a file and 3,782 even in API format. An agent that reads it pays
for all of it, every turn it stays in context, and then has to edit the right widget in the right subgraph
without breaking anything.

A recipe keeps the graph on disk. The agent reads a card instead: what the recipe does, the four or five
settings a person would change, and tips. That's about 150 tokens through `show`. The script puts the values
into the graph, and the agent never sees it.

This was the biggest single saving we found, bigger than any choice of protocol.

## MCP, a CLI, or let the agent write the requests?

We gave pi + Qwen 27B the same job five ways: make 3 pictures of a fox with the Z-Image Turbo template.

| Route | Time | Input tokens, all calls |
|---|---:|---:|
| This kit's 3-tool MCP server | 17 s | 11,067 |
| This kit's CLI + SKILL.md | 36 s | 28,266 |
| Raw HTTP: the agent writes its own script | 107 s | 269,358 |
| artokun comfyui-mcp, `--compact` | 174 s | 389,375 |
| Comfy's official comfy-mcp 0.11.0 | 166 s | 1,016,227 |

All five made the pictures. One run each, so read it as a demonstration, not a benchmark.

- **MCP isn't the problem; size is.** The cheapest route and the most expensive one were both MCP. A server
  sends its whole tool list with every call: 16,577 tokens for Comfy's official one, 50,247 for artokun's in
  default mode. Compact modes shrink the list but make the agent spend calls looking tools up.
- **Raw HTTP works when the workflow is tiny** (this template has 9 nodes), and the agent still read the workflow
  and node schemas to get there. With a 30-node workflow in subgraphs it's a different job. ComfyUI's full node
  list (`/object_info`) is about 4 million tokens on this machine, so an agent should never fetch it whole.
- **A CLI with a short skill file works with every agent**, because they all can run a command. It also covers
  the jobs you don't want in a chat app's tool list: installing, restarting, making recipes.
- **Use the MCP server where there's no shell:** the Claude and ChatGPT apps. Keep it to a few tools that each
  do a whole job.

## Ways to make an agent read less

1. Keep graphs out of the context. Convert once, store the API format, and let a script fill in values.
2. Show a short menu first (`list` is one line a recipe) and details only on request (`show`, then
   `show --notes`).
3. Few tools, each doing a whole job: `generate` queues, waits, downloads and reports in one call.
4. Return file paths and seeds, not JSON, logs or base64. Send a small preview only to clients that can't open
   files.
5. Batch in one call: `--count 4`, or a setting given more than once for different versions.
6. Keep the skill short and load it on demand. Ours is about 850 tokens, and pi, Claude Code and Codex read only a
   skill's one-line description until they need the rest.
7. Let the agent look at results, but not with a small model as the only judge. Each picture it opens costs
   tokens, and Qwen 27B twice reported a "two-headed fox" in a picture with one fox.
8. With a local model, watch the biggest single prompt, not just the total. Most of a long run is served from
   llama.cpp's prompt cache; what slows it down is a prompt that keeps growing.

## Converting workflows without a browser

The ComfyUI server only runs API-format jobs. The editor's Run button does the conversion in the browser.
comfy-cli can do it from the command line (`comfy run --workflow FILE --print-prompt`). We converted the 12
AusBoss example workflows both ways and compared them node by node: 7 were identical, 4 differed only by a
Workflow Switches node that does nothing at run time, and 1 (Simple Video Watermark Remover) came out with two
different settings. So: convert with comfy-cli, check each recipe with one run, and when it's wrong, use the
editor's *Export (API)* file. comfy-cli 1.29 came out the same day with a newer converter; we tested 1.23.

Anything that only happens in the editor (a JavaScript switch that bypasses groups, a button, a value computed in
the browser) is fixed at the state the workflow was saved in.

## Installing what a workflow needs, with one yes

A saved workflow already says what it needs. Each node records its pack (`cnr_id` or `aux_id`), and loader
nodes carry a `models` list with file names, folders and download links: the same list ComfyUI's "missing models"
dialog uses. `install` reads those, checks what this ComfyUI already has, prints the rest with sizes, and after one
yes downloads the models and clones the packs.

Don't trust automatic guessing. For Klein 9B Outpaint, `comfy node deps-in-workflow` named a third-party pack
the workflow doesn't use; the workflow's own `cnr_id` fields were right.

Downloads that need an account read `HF_TOKEN` (or `HF_API_TOKEN`) and `CIVITAI_API_TOKEN` from the
environment. Tokens never go into a recipe, a tool call or a log.

## A local model and ComfyUI on one card

Qwen3.8 27B (Q3_K_XL, about 19 GB with its vision part) stayed loaded while Wan 2.2 14B made a 5-second clip.
The clip took 39 s and the card peaked at 32.0 GB of 32.6, so there was almost nothing to spare. On a 24 GB card,
expect to unload one before running the other.

## Getting results to you

- **Phone:** Taildrop sends files straight to your phone over Tailscale (on an iPhone they wait in the Tailscale
  app); the gallery serves `outputs/` to your tailnet with Tailscale Serve; ntfy sends push notifications. See
  [phone.md](phone.md).
- **Claude and ChatGPT apps:** they call MCP servers from their own cloud, so your server needs a public HTTPS
  address. Being on your tailnet isn't enough. The kit runs behind Tailscale Funnel with a long secret, and never
  exposes ComfyUI's own port. Claude connectors added on the web also show up in the phone app. ChatGPT lets you
  add full custom MCP servers on Business, Enterprise and Edu workspaces, and read/fetch only on Pro (as of
  November 2026). See [chat-apps.md](chat-apps.md).
- **Comfy Cloud MCP** (`https://cloud.comfy.org/mcp`) is the easy way into the chat apps if you're fine with
  Comfy's GPUs and a Comfy Cloud subscription. It runs their templates and models, not your local setup.

## What else is out there

Stars and versions as of November 24, 2026.

| Project | What it is | Good for |
|---|---|---|
| [Comfy-Org/comfy-mcp](https://github.com/Comfy-Org/comfy-mcp) (375 stars, 0.11.0) | Comfy's official local MCP server, 41 tools, wraps comfy-cli | an agent exploring templates, models and nodes, and installing them |
| [Comfy Cloud MCP](https://docs.comfy.org/agent-tools/mcp) | hosted MCP on Comfy's GPUs | chat apps with no setup |
| [artokun/comfyui-mcp](https://github.com/artokun/comfyui-mcp) (953 stars, 0.52.206) | the biggest local MCP server, 41 tools, with a 3-tool compact mode | building and editing graphs from an agent |
| [joenorton/comfyui-mcp-server](https://github.com/joenorton/comfyui-mcp-server) (412 stars) | one tool per API workflow you drop in a folder | the same idea as recipes, over MCP |
| [Pixelle-MCP](https://github.com/ATH-MaaS/Pixelle-MCP) (1,132 stars) | workflows as MCP tools, with its own chat UI | a ready web UI |
| [ComfyUI_Skills_OpenClaw](https://github.com/HuangYuChuh/ComfyUI_Skills_OpenClaw) (424 stars) | skills plus a CLI for OpenClaw and others | the closest thing to this kit; needs API-format workflows |
| [comfy-cli](https://github.com/Comfy-Org/comfy-cli) (1,063 stars, 1.29.0) | Comfy's command line: install, run, convert, download | the engine under several of these, including this kit's `add` |

None of them, as far as we found, compared these routes on the same job with a local model. That's why we did.
