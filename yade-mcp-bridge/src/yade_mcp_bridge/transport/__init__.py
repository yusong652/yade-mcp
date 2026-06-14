# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""HTTP + SSE transport: the bridge's outward protocol surface.

Maps the request/response handler dict onto ``POST /<command>`` and serves the
server-push doorbell stream on ``GET /events``.
"""

from .server import YADEBridgeServer, create_server

__all__ = [
    "YADEBridgeServer",
    "create_server",
]
