# ChatGPT and Claude apps

`python3 comfy_agent.py mcp` serves the same recipes as an MCP server with three tools:

| Tool | Does |
|---|---|
| `recipes` | lists the recipes; with a name, shows one recipe's settings and tips |
| `generate` | `recipe`, `settings`, `count`, optional `send`: queues, waits, returns the text report plus a small JPEG preview of up to 4 pictures |
| `results` | collects jobs that were still running when `generate` returned |

Their definitions are about 300 tokens, against 16,577 for Comfy's official local MCP server and 50,247 for
artokun's comfyui-mcp (41 tools each).
Install and delete are deliberately not tools: those stay with you and the CLI.

## Coding agents (stdio)

Claude Code:

```bash
claude mcp add comfy -- python3 /path/to/comfy-agent/comfy_agent.py mcp
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.comfy]
command = "python3"
args = ["/path/to/comfy-agent/comfy_agent.py", "mcp"]
```

pi (`~/.pi/agent/mcp.json`):

```json
{"mcpServers": {"comfy": {"command": "python3", "args": ["/path/to/comfy-agent/comfy_agent.py", "mcp"], "exposure": "direct"}}}
```

For these agents the CLI with `SKILL.md` costs about the same and also covers installing and making recipes,
so the MCP server is optional.

## Claude and ChatGPT apps (remote MCP)

These apps call your MCP server from their own cloud, not from your phone, so the server needs a public HTTPS
address. Being on your tailnet isn't enough. That means putting a door to your GPU on the internet. Do it
only like this:

1. Start the server on this computer only, with a long secret:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"      # make a secret
   COMFY_AGENT_TOKEN=THE-SECRET python3 comfy_agent.py mcp --http 8765
   ```

   It listens on 127.0.0.1 only. It answers at `/mcp/THE-SECRET` (or `/mcp` with
   `Authorization: Bearer THE-SECRET`), and every other path gets a 404.

2. Publish just that port with Tailscale Funnel:

   ```bash
   tailscale serve status          # see which ports you already use
   tailscale funnel --bg --https=10000 http://127.0.0.1:8765
   ```

   Funnel only works on ports 443, 8443 and 10000, and it opens the whole port to the internet, including
   anything Tailscale Serve already has on it. Pick one that `tailscale serve status` doesn't list. Never funnel ComfyUI's own
   port (8188): its API can install code and read files.

3. Add the connector:
   - **Claude** (claude.ai on the web: Settings > Connectors > Add custom connector):
     `https://YOUR-PC.YOUR-TAILNET.ts.net:10000/mcp/THE-SECRET`. Connectors you add on the web also show up in
     the phone app.
   - **ChatGPT** (on the web, add a custom MCP server in the plugins/connectors settings). Check your plan
     first: as of November 2026, OpenAI documents full custom MCP servers for Business, Enterprise and Edu
     workspaces, and read/fetch-only access for Pro.

4. Turn it off when you're done: `tailscale funnel --https=10000 off`.

What to expect:

- The secret in the address is the only lock. Anyone with the address can use your GPU, so don't paste it
  anywhere public, and make a new one if it leaks. Both apps also support OAuth for connectors; this kit
  doesn't implement it.
- The server returns a JPEG preview of each picture. Whether the app shows it inline varies by app and
  version; the reliable route to your phone is still `send` (Taildrop or ntfy) or the gallery. Videos come
  back as file paths only.
- A tool call has to finish within the app's time limit. `generate` waits up to 240 seconds
  (`COMFY_AGENT_MCP_WAIT` changes it), then returns what's done; ask the app to call `results` for the rest.
