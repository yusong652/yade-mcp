#!/bin/bash
# Start YADE interactive console with bridge source and docs output mounted.
#
# Mounts:
#   1. yade_mcp_bridge source → Python site-packages (live-dev for bridge)
#   2. python_api_docs → /docs_output (target for doc scraper scripts that
#      run inside YADE and need to write regenerated JSON back onto the host
#      source tree)
#   3. workspace → /workspace (scratch dir for local script development;
#      edit on host, %run /workspace/xxx.py inside YADE; gitignored)

# Clean up: stop any container holding our ports (9002 bridge, 6080 noVNC).
# Filter by published port instead of ancestor — image-ID changes across
# rebuilds leave orphan containers that ancestor= won't match.
for port in 9002 6080; do
    ids=$(docker ps -q --filter "publish=${port}")
    [ -n "$ids" ] && docker rm -f $ids
done 2>/dev/null

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "${REPO_ROOT}/workspace"

# Load optional .env (for PROJ_MOUNT_SRC etc.) from repo root.
if [ -f "${REPO_ROOT}/.env" ]; then
    set -a; . "${REPO_ROOT}/.env"; set +a
fi

EXTRA_MOUNTS=()
if [ -n "${PROJ_MOUNT_SRC}" ]; then
    EXTRA_MOUNTS+=(-v "${PROJ_MOUNT_SRC}:/proj")
fi

# Git Bash / MSYS quirks:
#   - docker.exe sees no real TTY → wrap with winpty
#   - MSYS rewrites '/proj' inside '-v src:/proj' to a Windows path → disable pathconv
DOCKER=docker
if [ -n "$MSYSTEM" ]; then
    export MSYS_NO_PATHCONV=1
    command -v winpty >/dev/null && DOCKER="winpty docker"
fi

# No --platform here: the image is amd64 (pinned in Dockerfile.dev), but
# classic-builder images lack manifest platform metadata, so passing
# --platform makes docker fail local resolution and attempt a pull.
# The harmless "platform mismatch" warning at startup is expected on
# Apple Silicon.
$DOCKER run -it --rm -p 9002:9002 -p 6080:6080 \
    -v "${REPO_ROOT}/yade-mcp-bridge/src/yade_mcp_bridge:/usr/local/lib/python3.10/dist-packages/yade_mcp_bridge" \
    -v "${REPO_ROOT}/src/yade_mcp/knowledge/resources/python_api_docs:/docs_output" \
    -v "${REPO_ROOT}/workspace:/workspace" \
    "${EXTRA_MOUNTS[@]}" \
    yade-dev
