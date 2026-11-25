# comfy-agent

Let any AI agent make pictures and videos with your ComfyUI, from plain English.

> "Make me four pictures of a cabin in the snow, turn the best one into a video, and send them to my phone."

Claude Code, Codex, pi, OpenClaw, Cursor, or a small local model: they all can run a command, so they all can
use this. It's one Python file with no dependencies, a short instruction file for the agent, and **recipes**:
your workflows, each converted once into a form an agent can use without reading the whole graph.

**AI agents: read [SKILL.md](SKILL.md) and follow it.**

## Quick start

1. Start ComfyUI the way you normally do.
2. Get this folder and check that it can see ComfyUI:

   ```bash
   git clone https://github.com/ausboss/comfy-agent
   cd comfy-agent
   python3 comfy_agent.py doctor
   ```

3. Open your agent in this folder and ask for something:
   *"Make 4 pictures of a red fox asleep in the snow."*
   The agent reads `AGENTS.md` (or `CLAUDE.md`), which points it to `SKILL.md`.

To use it from any folder, install it as a skill instead (pick the line for your agent):

```bash
ln -s "$PWD" ~/.claude/skills/comfy-agent       # Claude Code
ln -s "$PWD" ~/.agents/skills/comfy-agent       # Codex
ln -s "$PWD" ~/.pi/agent/skills/comfy-agent     # pi
ln -s "$PWD" ~/.openclaw/workspace/skills/comfy-agent   # OpenClaw
```

The first time a recipe needs something you don't have, the agent shows you the list (node packs and model
files with their sizes) and asks once. After your yes it installs everything and carries on.

## What the agent runs

```text
comfy_agent.py list                          the recipes, one line each
comfy_agent.py show RECIPE                   its settings, defaults and tips
comfy_agent.py run RECIPE prompt="..." --count 4 [--send] [--no-wait]
comfy_agent.py wait                          collect jobs that are still running
comfy_agent.py send last                     send the last results to your phone
comfy_agent.py doctor | install RECIPE [--yes] | restart
comfy_agent.py add WORKFLOW.json --name NAME make a recipe from your own workflow
comfy_agent.py gallery                       a private web page of every result, on your tailnet
comfy_agent.py mcp [--http PORT --token T]   the same thing as a 3-tool MCP server
```

`run` queues separate jobs with separate seeds, so four results arrive one by one and each can be remade.
It finds model files wherever your ComfyUI keeps them (same file name, any subfolder), uploads the
pictures or clips you give it, waits, downloads every result to `outputs/RECIPE/`, and prints each path
with its seed.

## The recipes in this repo

| Recipe | What it does | Source |
|---|---|---|
| `zimage` | A picture from words, about 3 s each | ComfyUI's Z-Image Turbo template |
| `wan-i2v` | Animate a picture: a 5 s clip in about 40 s | ComfyUI's Wan 2.2 14B template, 4-step LoRA on |
| `qwen-edit` | Change a photo with a short request | AusBoss: Qwen Image 2.1 Edit + Realign |
| `klein-outpaint` | Make a picture bigger past its edges | AusBoss: Klein 9B Outpaint |

Times are from an RTX 5090. Each recipe lists its models and where to download them; `install` does it.

## Get results on your phone or another computer

- **Taildrop** (Tailscale): `python3 comfy_agent.py config phone=tailscale:YOUR-PHONE laptop=tailscale:YOUR-LAPTOP`,
  then add `--send` (or `--send laptop`) to any run. On an iPhone, open the Tailscale app to save the files.
- **Gallery**: `python3 comfy_agent.py gallery` serves `outputs/` on your tailnet with
  Tailscale Serve and prints the address. Open it in any browser on your phone or computers; only your own
  devices can reach it.
- **ntfy**: `python3 comfy_agent.py config phone=ntfy:SOME-LONG-RANDOM-TOPIC` sends each file as a push
  notification. On ntfy.sh, anyone who knows the topic can see the pictures, so use a long random
  name or your own ntfy server.

More in [docs/phone.md](docs/phone.md).

## A local model on a 16 GB card

A 27B model and an image model don't fit on a 16 GB card together, but they can take turns. Tell the kit how
to unload your model, and every run takes it off the card first, waits for the results, then frees ComfyUI's
memory so the model loads back on the agent's next turn:

```bash
python3 comfy_agent.py config unload_llm="curl -s http://127.0.0.1:8080/unload"     # llama-swap
```

Setup for llama-swap, LM Studio and Ollama, and what it costs in time: [docs/local-models.md](docs/local-models.md).

## ChatGPT and Claude apps

`comfy_agent.py mcp` is the same thing as an MCP server with three tools (`recipes`, `generate`,
`results`). For Claude Code, Codex or pi it runs over stdio. For the Claude and ChatGPT apps, on your
computer or your phone, it needs a public HTTPS address, so it runs behind Tailscale Funnel with a secret. Read
[docs/chat-apps.md](docs/chat-apps.md) before you do that: it puts your GPU on the internet.

## Your own workflows

```bash
python3 comfy_agent.py add "My Workflow.json" --name my-thing
```

This converts the workflow once (with comfy-cli, or from ComfyUI's *Export (API)* file), finds the
settings a person would change, the node packs it uses and the models it needs with their links, and
writes `recipes/my-thing/`. Then you (or your agent) tidy `card.json`: plain names, help lines, and
nothing that nobody should touch. See [docs/recipes.md](docs/recipes.md).

## Why a CLI and recipes, not a big MCP server

The agent pays for every token it reads, on every turn. Measured on the workflows in this repo:

| What the agent reads | Tokens |
|---|---|
| artokun comfyui-mcp's tool list (41 tools) | 50,247, every turn |
| Comfy's official comfy-mcp tool list (41 tools) | 16,577, every turn |
| One AusBoss workflow file (Klein 9B Outpaint) | 16,909 |
| The same workflow in API format | 3,782 |
| Its recipe card, through `show` | about 150 |
| `SKILL.md` | about 850 |

The same job ("make 3 pictures") with a local Qwen 27B in pi: 11,067 input tokens through this kit's MCP
server, 28,266 through the CLI, 389,375 through artokun's compact mode and 1,016,227 through Comfy's official
server. Details: [docs/tokens.md](docs/tokens.md). What we found along the way: [docs/research.md](docs/research.md).

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests run against a fake ComfyUI, so they need no GPU.
