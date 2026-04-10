#!/bin/bash
# Start YADE interactive console with bridge source and docs output mounted.
#
# Mounts:
#   1. yade_mcp_bridge source → Python site-packages (live-dev for bridge)
#   2. python_api_docs → /docs_output (target for doc scraper scripts that
#      run inside YADE and need to write regenerated JSON back onto the host
#      source tree)

# Clean up: stop any running yade-dev container and remove exited ones
docker rm -f $(docker ps -aq --filter ancestor=yade-dev) 2>/dev/null

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

docker run -it --rm -p 9002:9002 \
    -v "${REPO_ROOT}/yade-mcp-bridge/src/yade_mcp_bridge:/usr/local/lib/python3.10/dist-packages/yade_mcp_bridge" \
    -v "${REPO_ROOT}/src/yade_mcp/knowledge/resources/python_api_docs:/docs_output" \
    yade-dev
