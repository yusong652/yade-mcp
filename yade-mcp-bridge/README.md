# yade-mcp-bridge

[![PyPI](https://img.shields.io/pypi/v/yade-mcp-bridge)](https://pypi.org/project/yade-mcp-bridge/)

WebSocket bridge that runs inside a YADE process and enables execution tools for [yade-mcp](https://pypi.org/project/yade-mcp/).

## Quick Start

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

The bridge auto-detects the runtime: Qt timer in GUI mode, blocking poll in console mode.

Expected output (one line):

```text
YADE MCP Bridge on ws://localhost:9002, log: /your-working-dir/.yade-mcp/bridge.log
```

Detailed initialization logs go to `bridge.log` only (stdout shows warnings and errors).

## Options

```python
yade_mcp_bridge.start(
    host="localhost",           # Server host
    port=9002,                  # Server port
    mode="auto",                # "auto", "gui", or "console"
    interrupt_check_period=1,   # PyRunner checks every N iterations
)
```

## Requirements

- Python >= 3.8
- YADE with Python bindings
- `websockets >= 9.1, < 13`

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Port in use | `yade_mcp_bridge.start(port=9003)`, then set `YADE_MCP_BRIDGE_URL=ws://localhost:9003` |
| Connection failed | Check bridge is running, see `.yade-mcp/bridge.log` |
| PyRunner not available | YADE installation may lack PyRunner; interrupt checking during `O.run()` will be disabled |

## Development

Run the bridge from source inside YADE:

```python
import sys
sys.path.insert(0, '/path/to/yade-mcp/yade-mcp-bridge/src')
import yade_mcp_bridge
yade_mcp_bridge.start()
```

For full MCP client setup, see [yade-mcp](https://pypi.org/project/yade-mcp/).

License: MIT ([LICENSE](https://github.com/yusong652/yade-mcp/blob/main/LICENSE)).
