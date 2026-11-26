# Set up comfy-agent (for the agent doing the setup)

The person asked you to set this kit up. Do the checks yourself and ask only the questions below, one at a
time, each with a suggested answer they can accept.

## 0. Get the folder

Reuse a checkout if there is one; otherwise `git clone https://github.com/ausboss/comfy-agent` and work inside
it. It needs Python 3.9 or newer: `python3 --version` (on Windows use `python` or `py -3` in every command here).

## 1. Find ComfyUI

Run `python3 comfy_agent.py doctor`. It uses `COMFY_URL` if that is set, then a saved address, then
`http://127.0.0.1:8188`.
- If it answers, say which ComfyUI and GPU it found and go on.
- If not, ask: **"What address opens ComfyUI in a browser on this computer?"** Suggest
  `http://127.0.0.1:8188`; another computer looks like `http://192.168.1.20:8188`. Save it with
  `python3 comfy_agent.py config url=ADDRESS` and run `doctor` again. If an old `COMFY_URL` is set, it wins: unset it.
- Still nothing: ComfyUI isn't running, or (on another computer) it needs `--listen` and an open firewall
  port. Tell them; don't install or start ComfyUI yourself.

## 2. Make it available to their agent

Ask: **"Use this kit from any folder, or only inside this one?"** Suggest any folder. Then link this folder
into the skills folder of the agent they use, creating that folder first:

| Agent | Skills folder |
|---|---|
| Claude Code | `~/.claude/skills/` |
| Codex | `~/.agents/skills/` |
| pi | `~/.pi/agent/skills/` |
| OpenClaw | `~/.openclaw/workspace/skills/` |

Mac and Linux: `mkdir -p FOLDER && ln -s "$PWD" FOLDER/comfy-agent`. Windows (PowerShell):
`New-Item -Force -ItemType Directory FOLDER; New-Item -ItemType Junction -Path FOLDER\comfy-agent -Target (Get-Location).Path`.
Another agent that can run commands: open it in this folder (it reads `AGENTS.md`). An agent that only takes
MCP servers: add `python3 /full/path/comfy_agent.py mcp` as a stdio MCP server in its settings.

## 3. Make one picture

When `doctor` shows `zimage` ready, run
`python3 comfy_agent.py run zimage prompt="A red fox asleep in the snow" --count 1`, open the printed file, and
show it to them. If no recipe is ready, `doctor` says what is missing: go to step 5, then come back.

## 4. Their own workflows

Ask: **"Want me to turn one of your own workflows into a recipe?"** Suggest their most used one. Saved
workflows are in ComfyUI's `user/default/workflows/` folder on the computer that runs it; copy the file here
if that is another computer. Run `python3 comfy_agent.py add "WORKFLOW.json" --name NAME` and finish its card
as SKILL.md says. Without comfy-cli, `add` asks for an API export too: in ComfyUI, Workflow menu >
Export (API), then `add "WORKFLOW.json" --api "WORKFLOW_api.json" --name NAME`.

## 5. Installing what a recipe needs

Ask: **"When a recipe needs a model or node pack you don't have, should I install it without asking?"**
Suggest asking first. Save it: `python3 comfy_agent.py config install=ask` or `install=yes`. This is their
choice; the kit doesn't limit what or where they download. `install RECIPE` lists what is missing and
`install RECIPE --yes` installs it. Installing runs on the computer that runs ComfyUI.

## 6. Optional extras, last

One line each; set one up only if they want it now:
- Send results to their phone with Taildrop or ntfy. [Phone setup](docs/phone.md)
- Review results on their phone with the optional [comfy-agent-review add-on](https://github.com/ausboss/comfy-agent-review).
- Chat apps (ChatGPT, Claude) on the phone: [docs/chat-apps.md](docs/chat-apps.md).

Ask: **"Keep a private log on this computer of how much the kit adds to your agent's tokens? Nothing is sent
anywhere."** Suggest no. On a yes: `python3 comfy_agent.py config stats=on`. README, "Usage stats (optional)",
says what it keeps.

Then tell them to just ask in plain English, for example: "Make 4 pictures of a red fox asleep in the snow."
