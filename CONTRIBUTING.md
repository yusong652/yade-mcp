# Contributing

PRs and issues are welcome!

## Development Setup

```bash
git clone https://github.com/yusong652/yade-mcp.git
cd yade-mcp
uv sync --group dev
uv run yade-mcp
```

## Running Tests

```bash
pytest tests/ -v
```

## Bridge Development

To run the bridge from source inside YADE:

```python
import sys
sys.path.insert(0, '/path/to/yade-mcp/yade-mcp-bridge/src')
import yade_mcp_bridge
yade_mcp_bridge.start()
```

See [yade-mcp-bridge/README.md](yade-mcp-bridge/README.md) for bridge-specific options and troubleshooting.

## Architecture

```
Claude Code / Codex CLI / Gemini CLI
    │
    │  MCP (stdio/http)
    ▼
┌──────────┐
│ yade-mcp │
│  server  │
└────┬─────┘
     │  WebSocket
     ▼
┌────────────────┐
│ yade-mcp-bridge│  (runs inside YADE process)
│   ws://...:9002│
└────┬───────────┘
     │
     ▼
   YADE Engine
```

## Project Structure

```
yade-mcp/
├── src/yade_mcp/          # MCP server (documentation + execution tools)
├── yade-mcp-bridge/       # WebSocket bridge (runs inside YADE)
├── tests/                 # Test suite
└── assets/                # Header animation and generation script
```
