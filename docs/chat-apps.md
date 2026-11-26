# ChatGPT and Claude apps

`python3 comfy_agent.py mcp` serves the same recipes as an MCP server with three tools:

| Tool | Does |
|---|---|
| `recipes` | lists the recipes; with a name, shows one recipe's settings and tips |
| `generate` | `recipe`, `settings`, `count`, optional `send`: queues, waits, returns the text report plus a small JPEG preview of up to 4 pictures. With `redo` (a result's file) instead of `recipe`, it makes that result again with its settings and a new seed; `settings` change it |
| `results` | collects pending jobs and returns their results. |

Their definitions are about 390 tokens, against 16,577 for Comfy's official local MCP server and 50,247 for
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

## Claude Desktop and the ChatGPT app on the same computer (stdio)

When the app runs on the computer that runs ComfyUI, it starts the kit itself. You don't need a public address.

Claude Desktop: add this to `claude_desktop_config.json` and restart the app. It's in `~/.config/Claude/` on
Linux, `~/Library/Application Support/Claude/` on macOS and `%APPDATA%\Claude\` on Windows.

```json
{"mcpServers": {"comfy": {"command": "python3", "args": ["/path/to/comfy-agent/comfy_agent.py", "mcp"]}}}
```

The ChatGPT app's Codex side, set to "This computer", reads `~/.codex/config.toml`, so the Codex lines above
work there. The first call asks "Allow the comfy MCP server to run tool ...". Pick Always allow, or add this:

```toml
[mcp_servers.comfy.tools.recipes]
approval_mode = "approve"

[mcp_servers.comfy.tools.generate]
approval_mode = "approve"

[mcp_servers.comfy.tools.results]
approval_mode = "approve"
```

Don't set `approval_policy = "never"` for this: the app then turns the tool call down instead of asking.

We tested both on Linux, in fresh app profiles, in November 2026. Asked in plain English for 2 pictures, each app
called `generate`, showed the pictures in the chat and gave the file paths.

## Claude and ChatGPT apps on a phone or another computer (remote MCP)

From a phone or another computer, these apps call your MCP server from their own cloud, so the server needs a public HTTPS
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
   tailscale serve status          # see what you already serve on 443
   tailscale funnel --bg http://127.0.0.1:8765
   ```

   Claude only connects to port 443 (its help page: "Claude can't connect to a URL that specifies another
   port"). Funnel opens the whole port to the internet, including anything Tailscale Serve already has on it,
   so if `tailscale serve status` lists something on 443, don't do this on that machine. Never funnel
   ComfyUI's own port (8188): its API can install code and read files.

3. Add the connector:
   - **Claude** (claude.ai on the web: Settings > Connectors > Add custom connector):
     `https://YOUR-PC.YOUR-TAILNET.ts.net/mcp/THE-SECRET`. Connectors you add on the web also show up in
     the phone app.
   - **ChatGPT** (on the web, add a custom MCP server in the plugins/connectors settings). Check your plan
     first: as of November 2026, OpenAI documents custom MCP servers only for Business, Enterprise and Edu
     workspaces.

4. Turn it off when you're done: `tailscale funnel --https=443 off`.

What to expect:

- The secret in the address is the only lock. Anyone with the address can use your GPU, so don't paste it
  anywhere public, and make a new one if it leaks. Both apps also support OAuth for connectors; this kit
  doesn't implement it.
- The server returns a JPEG preview of each picture. Whether the app shows it inline varies by app and
  version; the reliable route to your phone is still `send` (Taildrop or ntfy). Videos come
  back as file paths only.
- A tool call has to finish within the app's time limit. `generate` waits up to 240 seconds
  (`COMFY_AGENT_MCP_WAIT` changes it), then returns what's done; ask the app to call `results` for the rest.
