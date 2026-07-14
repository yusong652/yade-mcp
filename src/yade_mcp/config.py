"""Runtime configuration for YADE MCP server."""

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class BridgeConfig:
    url: str
    sse_retry_interval_s: float
    request_timeout_s: float


def get_bridge_config() -> BridgeConfig:
    """Load bridge config from environment variables."""
    return BridgeConfig(
        url=os.getenv("YADE_MCP_BRIDGE_URL", "http://localhost:9002"),
        sse_retry_interval_s=_env_float("YADE_MCP_SSE_RETRY_INTERVAL_S", 0.5),
        request_timeout_s=max(1.0, _env_float("YADE_MCP_REQUEST_TIMEOUT_S", 10.0)),
    )
