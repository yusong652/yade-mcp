# CLAUDE.md

## Project overview

yade-mcp is an MCP server that connects AI agents to YADE (open-source DEM engine). Two packages live in this monorepo:

- `yade-mcp` (src/yade_mcp/) — MCP server with 7 tools (2 doc + 5 execution)
- `yade-mcp-bridge` (yade-mcp-bridge/) — WebSocket bridge running inside YADE process

## Build and test

```bash
uv sync --group dev                    # install dependencies
uv run ruff check src/                 # lint MCP server
uv run ruff check yade-mcp-bridge/src/ # lint bridge (uses bridge's own ruff config)
uv run ruff format src/                # format
uv run mypy src/yade_mcp/              # type check
uv run pytest tests/ -v --ignore=tests/test_tools_integration.py  # unit + protocol tests (no YADE needed)
uv run pytest tests/ -v                # all tests (integration tests need YADE + bridge)
```

## Branch strategy

- `master` — code only, kept lightweight
- `assets` — large binary files (header.gif, header.blend, generation scripts)

Do NOT commit large files (images, .blend, etc.) to master.

## Release workflow

Tag-based via GitHub Actions:
- `v*` tags → publish `yade-mcp` to PyPI (e.g., `git tag -a v0.1.3 -m "..."`)
- `bridge-v*` tags → publish `yade-mcp-bridge` to PyPI (e.g., `git tag -a bridge-v0.1.1 -m "..."`)

Both use trusted publishing (PyPI OIDC). Version is defined in `__init__.py` of each package.

## Coding conventions

- Ruff for linting and formatting (N818: exception names must end with `Error`)
- Bridge has its own ruff config in `yade-mcp-bridge/pyproject.toml` (targets py38, ignores UP032/SIM115)
- Async-first: bridge client uses websockets with asyncio
- Response envelope: all tools return via `build_ok()` / `build_error()` from contracts.py

## Test structure

- `tests/` — MCP server unit tests (config, contracts, formatting, search, response truncation)
- `tests/bridge/` — Bridge unit + protocol tests (no YADE runtime needed)
  - `test_helpers.py`, `test_signals.py`, `test_utils.py`, `test_execution.py`, `test_tasks.py` — unit tests
  - `test_handlers.py` — handler tests with mock context
  - `test_protocol.py` — real WebSocket server, tests message format and routing
- `tests/test_bridge_client.py` — MCP bridge client (needs running bridge)
- `tests/test_tools_integration.py` — end-to-end MCP tools (needs YADE + bridge)

## Key architectural decisions

- BM25 search rebuilds index per query (~50ms for 350 docs, no stale state)
- README header GIF is served from assets branch via raw.githubusercontent.com URL
