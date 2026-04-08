# yade-mcp Agent Bootstrap Guide

Use this guide when an agent needs to set up `yade-mcp` execution end-to-end on a Linux machine.

## Target Outcome

1. MCP client is configured to run `yade-mcp`.
2. `yade-mcp-bridge` is installed in the system Python environment used by YADE.
3. Bridge is started inside a YADE process.
4. MCP execution tools are verified with `yade_execute_code`.

## Agent Execution Rules

- YADE uses the system Python (`/usr/bin/python3`). Install packages there.
- Always use `--user` flag with pip to avoid permission issues.
- If a step fails, report the exact command and output, then apply the next fallback.
- Respect step ownership labels:
  - `[AGENT]` means the agent should execute the action.
  - `[USER ACTION REQUIRED]` means the user must execute it manually.

## Step 1 - Configure MCP Client

[AGENT]

Use the client-specific Step 1 profile:

- Claude Code: https://raw.githubusercontent.com/yusong652/yade-mcp/master/docs/agentic/yade-mcp-bootstrap-claude.md
- Codex: https://raw.githubusercontent.com/yusong652/yade-mcp/master/docs/agentic/yade-mcp-bootstrap-codex.md
- Gemini CLI: https://raw.githubusercontent.com/yusong652/yade-mcp/master/docs/agentic/yade-mcp-bootstrap-gemini.md
- GitHub Copilot CLI: https://raw.githubusercontent.com/yusong652/yade-mcp/master/docs/agentic/yade-mcp-bootstrap-copilot.md
- OpenCode: https://raw.githubusercontent.com/yusong652/yade-mcp/master/docs/agentic/yade-mcp-bootstrap-opencode.md

If raw URL fetch is unavailable, use repository-relative paths:

- `docs/agentic/yade-mcp-bootstrap-claude.md`
- `docs/agentic/yade-mcp-bootstrap-codex.md`
- `docs/agentic/yade-mcp-bootstrap-gemini.md`
- `docs/agentic/yade-mcp-bootstrap-copilot.md`
- `docs/agentic/yade-mcp-bootstrap-opencode.md`

Apply this MCP launch contract in your client's native config format:

- server id/name: `yade-mcp`
- primary launch command: `uvx yade-mcp`
- fallback launch command: `uv tool run yade-mcp`
- enable server in client config
- prefer workspace-level config by default; use global config only if user explicitly requests it

When editing MCP config, use this order:

1. If config file does not exist, create it.
2. If config exists but has no `yade-mcp` entry, merge/add only that entry.
3. If `yade-mcp` already exists, validate/update only MCP launch fields (`command`, `args`, and client-specific extras).
4. Do not overwrite unrelated MCP servers.

## Step 2 - Install Bridge in System Python

[AGENT]

YADE links to the Python interpreter it was compiled against, which may differ from the default `python3` or `pip3` on the system (e.g. conda can shadow system Python). To install into the correct environment:

1. Determine which Python YADE uses. Check the YADE launcher script or try common paths:

```bash
head -1 "$(which yade 2>/dev/null)"   # shebang shows the interpreter
python3.8 --version 2>/dev/null       # common on Ubuntu 20.04
python3.10 --version 2>/dev/null      # common on Ubuntu 22.04
```

2. Install using that specific interpreter:

```bash
python3.8 -m pip install --user yade-mcp-bridge
```

If you cannot determine YADE's Python version, `pip3 install --user yade-mcp-bridge` is a reasonable default.

3. Verify:

```bash
python3.8 -c "import yade_mcp_bridge; print(yade_mcp_bridge.__version__)"
```

## Step 3 - Start Bridge in YADE

Ask the user to start YADE and run the following in the YADE Python console:

```python
import yade_mcp_bridge
yade_mcp_bridge.start()
```

Expected output includes:

- `YADE MCP Bridge Server`
- `ws://localhost:9002`

After the bridge is started, restart the client session before Step 4.

## Step 4 - Verify from MCP Client

[AGENT]

Reconnect MCP client and call:

- `yade_execute_code` with a simple snippet, e.g. `print('hello from YADE')`

If `yade_*` MCP tools are not visible in the client, ask user to fully restart client session first, then retry.

Success example (shape may vary by client):

```json
{
  "ok": true,
  "data": {
    "output": "hello from YADE\n"
  }
}
```

`ok: true` means the full MCP → bridge → YADE pipeline is working.

## Troubleshooting

- `Connection refused` / `bridge_unavailable`:
  - Bridge not running in YADE, or port `9002` not available.
- `No module named yade_mcp_bridge`:
  - Package not installed in system Python. Run `pip3 install --user yade-mcp-bridge`.
- `No module named websockets`:
  - Install websockets: `pip3 install --user websockets`.
- `yade_*` tools missing in client after setup:
  - Client session was not fully restarted after Step 1. Close/reopen client session and retry Step 4.
- Port conflict (another service on 9002):
  - Start bridge with custom port: `yade_mcp_bridge.start(port=9003)`.
