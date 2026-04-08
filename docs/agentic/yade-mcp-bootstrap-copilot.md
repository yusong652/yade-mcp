# yade-mcp Bootstrap (GitHub Copilot CLI)

Use this profile when the client is GitHub Copilot CLI.

## Step 1 (GitHub Copilot CLI) - Configure MCP

[AGENT]

Default target: workspace `.mcp.json`.
Use user-level `~/.copilot/mcp-config.json` only if user explicitly asks for global sharing.

Add/merge this MCP entry:

```json
{
  "mcpServers": {
    "yade-mcp": {
      "command": "uvx",
      "args": ["yade-mcp"]
    }
  }
}
```

If `uvx` is unavailable, fallback to:

```json
{
  "mcpServers": {
    "yade-mcp": {
      "command": "uv",
      "args": ["tool", "run", "yade-mcp"]
    }
  }
}
```

[USER ACTION REQUIRED]

Always close and restart the Copilot CLI session before continuing. Exit the current session and start a new one.

## Continue with common bootstrap

After Step 1, continue from Step 2 in:

- `docs/agentic/yade-mcp-bootstrap.md`
