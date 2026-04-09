#!/bin/bash
# Start YADE interactive console with bridge source mounted

# Clean up: stop any running yade-dev container and remove exited ones
docker rm -f $(docker ps -aq --filter ancestor=yade-dev) 2>/dev/null

docker run -it --rm -p 9002:9002 \
    -v "$(cd "$(dirname "$0")/.." && pwd)/yade-mcp-bridge/src/yade_mcp_bridge:/usr/local/lib/python3.10/dist-packages/yade_mcp_bridge" \
    yade-dev
