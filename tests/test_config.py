"""Tests for runtime configuration."""

from yade_mcp.config import get_bridge_config


class TestBridgeConfig:
    def test_defaults(self):
        cfg = get_bridge_config()
        assert cfg.url == "http://localhost:9002"
        assert cfg.request_timeout_s >= 1.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("YADE_MCP_BRIDGE_URL", "http://custom:9999")
        monkeypatch.setenv("YADE_MCP_REQUEST_TIMEOUT_S", "30.0")
        cfg = get_bridge_config()
        assert cfg.url == "http://custom:9999"
        assert cfg.request_timeout_s == 30.0

    def test_invalid_float_uses_default(self, monkeypatch):
        monkeypatch.setenv("YADE_MCP_REQUEST_TIMEOUT_S", "not_a_number")
        cfg = get_bridge_config()
        assert cfg.request_timeout_s == 10.0

    def test_timeout_minimum_enforced(self, monkeypatch):
        monkeypatch.setenv("YADE_MCP_REQUEST_TIMEOUT_S", "0.1")
        cfg = get_bridge_config()
        assert cfg.request_timeout_s >= 1.0
