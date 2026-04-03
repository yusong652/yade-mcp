# yade-mcp Bootstrap (Codex)

Use this profile when the client is OpenAI Codex CLI/IDE.

## Step 1 (Codex) - Configure MCP

[AGENT]

Default target: project-level `.codex/config.toml`.
Use global `~/.codex/config.toml` only if the user explicitly asks for global sharing.

Add/merge this MCP entry:

```toml
[mcp_servers.yade-mcp]
command = "uvx"
args = ["yade-mcp"]
```

If `uvx` is unavailable, fallback to:

```toml
[mcp_servers.yade-mcp]
command = "uv"
args = ["tool", "run", "yade-mcp"]
```

[USER ACTION REQUIRED]

Always close and reopen Codex session before continuing.

Optional verification:

- In Codex TUI: `/mcp`
- In terminal: `codex mcp --help` (and `codex mcp list` if available)

## Continue with common bootstrap

After Step 1, continue from Step 2 in:

- `docs/agentic/yade-mcp-bootstrap.md`
