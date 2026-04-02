# yade-mcp

<p align="center">
  <img src="https://raw.githubusercontent.com/yusong652/yade-mcp/master/assets/header.gif" alt="yade-mcp header" width="720">
</p>

[English](https://github.com/yusong652/yade-mcp/blob/main/README.md) | [简体中文](https://github.com/yusong652/yade-mcp/blob/main/README.zh-CN.md)

[![PyPI](https://img.shields.io/pypi/v/yade-mcp)](https://pypi.org/project/yade-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)

`O.engines += [LLM()]  # yet another engine.`

**yade-mcp** connects AI agents to [YADE](https://yade-dem.org/) — the open-source discrete element method engine — through the [Model Context Protocol](https://modelcontextprotocol.io/). Browse API docs, run simulations, and execute code, all through natural conversation.

## Tools (7)

**2 documentation tools** — browse and search the YADE Python API with 350+ class docs and BM25 keyword search. No bridge required.

**5 execution tools** — synchronous REPL, async task submission, progress monitoring, interruption, and task history. Requires bridge.

## Quick Start

### Prerequisites

- **[YADE](https://yade-dem.org/doc/installation.html)** installed
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** installed (for `uvx`)

### Manual Setup

**1. Register the MCP server** in your client config:

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

**2. Start the bridge inside YADE:**

```bash
pip install yade-mcp-bridge
```

Then in a YADE Python console:

```python
import yade_mcp_bridge
yade_mcp_bridge.start()
```

### Verify

Restart your AI agent (Claude Code, Codex CLI, Gemini CLI, etc.) and ask it to call `yade_execute_code` to verify the connection.

## Features

- **350+ class documentation** — covers ~90% of the YADE Python API, enriched with real types, defaults, and docstrings
- **Hierarchical API browsing** — agents navigate categories, subcategories, and classes with progressive disclosure, reducing hallucination
- **BM25 keyword search** — fast, ranked search across all API docs by natural language queries
- **Synchronous REPL** — rapid iteration for querying simulation state (`O.bodies`, `O.iter`, quick tests)
- **Async task lifecycle** — submit long-running simulations, monitor progress, gracefully interrupt via PyRunner, and browse history
- **Multi-client compatible** — works with Claude Code, Codex CLI, Gemini CLI, OpenCode, and other MCP clients

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT — see [LICENSE](LICENSE).
