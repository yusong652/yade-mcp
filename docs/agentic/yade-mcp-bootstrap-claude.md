# yade-mcp Bootstrap (Claude Code)

Use this profile when the client is Claude Code.

## Step 1 (Claude Code) - Configure MCP

[AGENT]

Default target: workspace `.mcp.json`.
Use user-level MCP config only if user explicitly asks for global sharing.

Add/merge this MCP entry:

```json
{
  "mcpServers": {
    "yade-mcp": {
      "type": "stdio",
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
      "type": "stdio",
      "command": "uv",
      "args": ["tool", "run", "yade-mcp"]
    }
  }
}
```

## Continue with common bootstrap

After Step 1, continue from Step 2 in:

- `docs/agentic/yade-mcp-bootstrap.md`
