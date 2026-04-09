#!/bin/bash
# Start YADE interactive console with bridge source mounted
docker run -it --rm -p 9002:9002 \
    -v "$(cd "$(dirname "$0")/.." && pwd)/yade-mcp-bridge/src/yade_mcp_bridge:/usr/local/lib/python3.10/dist-packages/yade_mcp_bridge" \
    yade-dev
