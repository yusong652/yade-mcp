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

**yade-mcp** connects AI agents to [YADE](https://yade-dem.org/), the open-source discrete element method engine, through the [Model Context Protocol](https://modelcontextprotocol.io/). The agent browses the YADE API, runs code in a live YADE session, launches long simulations as background tasks, and reads what you type at the YADE console.

![yade-mcp demo](https://raw.githubusercontent.com/yusong652/yade-mcp/assets/assets/demo.gif)

## Tools (7)

Two documentation tools (no bridge needed) and five execution tools (bridge required):

| Tool | Purpose | Bridge |
| --- | --- | --- |
| `yade_browse_api` | Walk the YADE Python class tree | No |
| `yade_query_api` | BM25 keyword search across the API | No |
| `yade_execute_code` | Run Python in the live YADE process; returns synchronously | Yes |
| `yade_execute_task` | Submit a script as a long-running background task | Yes |
| `yade_check_task_status` | Inspect a running or finished task (output, status) | Yes |
| `yade_interrupt_task` | Stop a running task at an iteration boundary, or cancel a queued one | Yes |
| `yade_list_tasks` | List submitted tasks with metadata | Yes |

## Example Prompts

- *"Set up a triaxial compression test on a dense packing and plot deviatoric stress against axial strain"*
- *"Build an irregular particle as a level set body and drop it onto a plane"*
- *"The simulation is still running, check the unbalanced force without stopping it"*
- *"Look up how GlobalStiffnessTimeStepper picks the timestep, then add it to this model"*
- *"The command I just typed in the console raised an error, what went wrong?"*
- *"List what was run yesterday and summarize what each task produced"*

## First-time Setup

### Prerequisites

- **[YADE](https://yade-dem.org/doc/installation.html)** installed
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** installed. It provides the `uvx` launcher that agent clients use to run `yade-mcp`; without it the client reports `No such file or directory` when starting the server.
- **An AI agent**: Claude Code, Codex CLI, Gemini CLI, or any MCP-capable client

### Agentic Setup (Recommended)

Copy this to your AI agent and let it self-configure:

```text
Fetch and follow this bootstrap guide end-to-end:
https://raw.githubusercontent.com/yusong652/yade-mcp/master/docs/agentic/yade-mcp-bootstrap.md
```

### Manual Setup

**1. Register the MCP server** with your agent (use the line for yours):

```bash
# Claude Code
claude mcp add yade-mcp -- uvx yade-mcp

# Codex CLI
codex mcp add yade-mcp -- uvx yade-mcp

# Gemini CLI
gemini mcp add yade-mcp uvx yade-mcp
```

Or fill in the MCP config file by hand:

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

**2. Install the bridge into YADE's Python.**

YADE embeds one specific interpreter (the system `python3` for the Debian and Ubuntu packages, or the one it was built against). A conda or venv Python is a different interpreter, and a package installed there is invisible to YADE. The reliable way to hit the right one is to install from inside YADE, where `sys.executable` is that interpreter. In the YADE console:

```python
import sys, subprocess
subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "yade-mcp-bridge"])
```

Two things can go wrong here:

- `No module named pip`: the interpreter has no pip. Install it with the system package manager, for example `sudo apt install python3-pip`.
- `externally-managed-environment` (Debian 12, Ubuntu 23.04 and later): pip refuses `--user` by default. Add `"--break-system-packages"` after `"--user"` in the command above.

Then exit and restart YADE so the new package directory is picked up.

The bridge is proposed for inclusion in YADE itself ([yade-dev/trunk !1187](https://gitlab.com/yade-dev/trunk/-/merge_requests/1187)). Once it ships with YADE, this step goes away and the bridge starts with `from yade import mcpbridge`.

**3. Start the bridge** in the YADE console:

```python
import yade_mcp_bridge
yade_mcp_bridge.start()
```

It prints one line: `YADE MCP Bridge on http://localhost:9002, log: <cwd>/.yade-mcp/bridge.log`.

### Verify

Restart your AI agent and ask it to check that it is connected to YADE. It calls `yade_execute_code`; `ok: true` in the response means the whole chain works.

## Daily Startup

Once first-time setup is done, each new YADE session only needs the bridge started again. In the YADE console:

```python
import yade_mcp_bridge
yade_mcp_bridge.start()
```

The MCP client config persists.

**Ports and containers.** `start()` takes `port` (default 9002) and `host`. If you change the port, the MCP server must be told, or it keeps connecting to 9002. Re-register it with the matching URL:

```bash
codex mcp remove yade-mcp
codex mcp add yade-mcp -- uvx yade-mcp --bridge-url http://localhost:9008
```

Inside a container, start with `yade_mcp_bridge.start(host="0.0.0.0")` so the bridge is reachable from outside.

## Features

- **Live REPL in the running YADE process**: `yade_execute_code` runs Python in the session's own namespace, so state persists between calls. It keeps working while a task runs, for reading intermediate results without stopping the simulation.
- **Task lifecycle**: submit a script as a background task, tail its output, stop it at an iteration boundary, fix and resubmit. Tasks queue up and run one at a time, so a multi-stage pipeline can be submitted in one go.
- **Task history across sessions**: every task's script, output, and final state stay on record. A new agent session lists what was run and picks up without being told.
- **Console input reaches the agent**: lines you type at the YADE console arrive in the agent's context on its next call, so you can work at the console and with the agent at the same time.
- **API documentation without a bridge**: the class tree and a BM25 search over the YADE Python API work offline, from a corpus refreshed against current YADE releases.
- **Multi-client**: works with Claude Code, Codex CLI, Gemini CLI, GitHub Copilot CLI, OpenCode, toyoura-nagisa, and other MCP clients.

## Troubleshooting

See [Troubleshooting](docs/agentic/yade-mcp-bootstrap.md#troubleshooting) in the bootstrap guide.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT, see [LICENSE](LICENSE).
