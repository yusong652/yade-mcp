#!/bin/bash
# Start an in-container X stack so YADE's Qt GUI is reachable via noVNC,
# then exec yade so its REPL + bridge thread run in the foreground.
#
# Services:
#   Xvfb :1           virtual framebuffer, depth 24, 1280x800
#   fluxbox           minimal window manager (so Qt windows are movable)
#   x11vnc            mirrors :1 to VNC on :5900
#   websockify        serves noVNC at http://localhost:6080/vnc.html
#
# Software GL via llvmpipe (no GPU in Docker-on-Mac).

set -e

export DISPLAY=:1
export LIBGL_ALWAYS_SOFTWARE=1
export QT_X11_NO_MITSHM=1

Xvfb :1 -screen 0 1280x800x24 -nolisten tcp &
XVFB_PID=$!

# Wait for Xvfb to accept connections.
for _ in $(seq 1 30); do
    if xdpyinfo -display :1 >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done

fluxbox >/dev/null 2>&1 &
x11vnc -display :1 -forever -shared -nopw -quiet -rfbport 5900 >/dev/null 2>&1 &
websockify --web=/usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

echo "noVNC ready: http://localhost:6080/vnc.html"

exec yade "$@"
