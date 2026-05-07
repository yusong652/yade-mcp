# yade-mcp

<!-- mcp-name: io.github.yusong652/yade-mcp -->

<p align="center">
  <img src="https://raw.githubusercontent.com/yusong652/yade-mcp/assets/assets/header.gif" alt="yade-mcp header" width="720">
</p>

[English](https://github.com/yusong652/yade-mcp/blob/master/README.md) | [简体中文](https://github.com/yusong652/yade-mcp/blob/master/README.zh-CN.md)

[![PyPI](https://img.shields.io/pypi/v/yade-mcp)](https://pypi.org/project/yade-mcp/)
[![Downloads](https://static.pepy.tech/badge/yade-mcp)](https://pepy.tech/project/yade-mcp)
[![GitHub stars](https://img.shields.io/github/stars/yusong652/yade-mcp)](https://github.com/yusong652/yade-mcp/stargazers)
[![Glama](https://glama.ai/mcp/servers/yusong652/yade-mcp/badges/score.svg)](https://glama.ai/mcp/servers/yusong652/yade-mcp)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)

`O.engines += [LLM()]  # yet another engine.`

**yade-mcp** connects AI agents to [YADE](https://yade-dem.org/) — the open-source discrete element method engine — through the [Model Context Protocol](https://modelcontextprotocol.io/). Browse API docs, run simulations, and execute code, all through natural conversation.

Your agent doesn't just call tools — it sits at your YADE console, runs long simulations on its own, and stays in sync with what you're doing.

![yade-mcp demo](https://raw.githubusercontent.com/yusong652/yade-mcp/assets/assets/demo.gif)

*Works with any MCP client — verified with Claude Code, Codex CLI, Gemini CLI, GitHub Copilot CLI, OpenCode, and toyoura-nagisa.*

## Features

### Your agent types, YADE runs

*Powered by `yade_execute_code`*

Describe what you want in plain language. The agent types commands into your YADE console — inspecting particles, tweaking parameters, stepping the engine, analyzing results. It reads each output, debugs, and iterates, the same way you do at the console yourself.

### Set it running, walk away

*Powered by `yade_execute_task` + `yade_check_task_status` + `yade_interrupt_task`*

Run a full YADE script as a background task — just like firing off `yade script.py`, except you don't have to babysit it. The agent watches on its own: tailing the live output, catching errors as they appear, stopping the run gracefully when something looks off, fixing the script, and resubmitting — until the simulation actually finishes.

### New session, no cold start

*Powered by `yade_list_tasks` + `yade_check_task_status`*

Every task you've submitted — the script, the live output, the final state — stays on record. When the context window fills up or you come back the next day, a fresh agent walks into a project that already remembers itself: it lists what's been run, reads what each task produced, and picks up without you re-explaining anything.

### A live shell into the running simulation

*Powered by `yade_execute_code`*

While a task runs, the agent has a live shell into the simulation — ask it to inspect any variable, dump any object's state, or render a fresh plot on demand, without editing the script or stopping the run.

### You type, the agent's in sync

Beyond submitted tasks, every line you type into the YADE console — the variables you peeked at, the parameters you tested, the dead ends you walked away from — flows into the agent's context too. When you turn to chat, it already has the trail of what you've been trying. Learning YADE and want feedback on what you just typed? Stuck on an unexpected error? Just ask — the agent saw what you typed and how YADE answered.

## Tools (7)

Two documentation tools (no bridge) and five execution tools (bridge required):

| Tool | Purpose | Bridge |
| --- | --- | --- |
| `yade_browse_api` | Walk the YADE Python class tree | No |
| `yade_query_api` | BM25 keyword search across the API | No |
| `yade_execute_code` | Run Python in the live YADE process; returns synchronously | Yes |
| `yade_execute_task` | Submit a script as a long-running background task | Yes |
| `yade_check_task_status` | Inspect a running or finished task (output, status) | Yes |
| `yade_interrupt_task` | Gracefully stop a running task | Yes |
| `yade_list_tasks` | List submitted tasks with metadata | Yes |

## Quick Start

### Prerequisites

- **[YADE](https://yade-dem.org/doc/installation.html)** installed
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** installed (for `uvx`)

### Agentic Setup (Recommended)

Copy this to your AI agent and let it self-configure:

```text
Fetch and follow this bootstrap guide end-to-end:
https://raw.githubusercontent.com/yusong652/yade-mcp/master/docs/agentic/yade-mcp-bootstrap.md
```

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

In a YADE Python console, install the bridge using YADE's own interpreter:

```python
import sys, subprocess
subprocess.check_call([
    sys.executable, "-m", "pip", "install", "--user",
    "--break-system-packages", "yade-mcp-bridge",
])
```

Restart YADE, then in the Python console:

```python
import yade_mcp_bridge
yade_mcp_bridge.start()
```

### Verify

Restart your AI agent (Claude Code, Codex CLI, Gemini CLI, etc.) and ask it to call `yade_execute_code` to verify the connection.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT — see [LICENSE](LICENSE).
