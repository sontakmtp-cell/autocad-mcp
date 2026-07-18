"""Tests for the Phase 1 transport configuration and entrypoint dispatch."""

from __future__ import annotations

import pytest

from autocad_mcp.config import load_transport_config


def test_stdio_is_the_default_transport(monkeypatch):
    for name in (
        "AUTOCAD_MCP_TRANSPORT",
        "AUTOCAD_MCP_HOST",
        "AUTOCAD_MCP_PORT",
        "AUTOCAD_MCP_PATH",
        "AUTOCAD_MCP_STATELESS_HTTP",
    ):
        monkeypatch.delenv(name, raising=False)

    config = load_transport_config()

    assert config.transport == "stdio"
    assert config.host == "127.0.0.1"
    assert config.port == 8765
    assert config.path == "/mcp"
    assert config.stateless_http is False


def test_http_values_are_normalized(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_TRANSPORT", "STREAMABLE-HTTP")
    monkeypatch.setenv("AUTOCAD_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("AUTOCAD_MCP_PORT", "9876")
    monkeypatch.setenv("AUTOCAD_MCP_PATH", "mcp/")
    monkeypatch.setenv("AUTOCAD_MCP_STATELESS_HTTP", "true")

    config = load_transport_config()

    assert config.transport == "streamable-http"
    assert config.port == 9876
    assert config.path == "/mcp"
    assert config.stateless_http is True


def test_invalid_transport_is_rejected(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_TRANSPORT", "tcp")

    with pytest.raises(ValueError, match="Unsupported AUTOCAD_MCP_TRANSPORT"):
        load_transport_config()


def test_invalid_port_is_rejected(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_PORT", "70000")

    with pytest.raises(ValueError, match="between 1 and 65535"):
        load_transport_config()


def test_invalid_boolean_is_rejected(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_STATELESS_HTTP", "sometimes")

    with pytest.raises(ValueError, match="must be a boolean"):
        load_transport_config()


def test_main_keeps_stdio_default(monkeypatch):
    import autocad_mcp.server as server_module

    calls = []

    def fake_run(*, transport):
        calls.append(transport)

    monkeypatch.delenv("AUTOCAD_MCP_TRANSPORT", raising=False)
    monkeypatch.setattr(server_module.mcp, "run", fake_run)

    server_module.main()

    assert calls == ["stdio"]


def test_main_dispatches_streamable_http(monkeypatch):
    import autocad_mcp.server as server_module

    captured = []

    def fake_run_http_server(config):
        captured.append(config)

    monkeypatch.setenv("AUTOCAD_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setattr(
        "autocad_mcp.http_server.run_http_server",
        fake_run_http_server,
    )

    server_module.main()

    assert len(captured) == 1
    assert captured[0].transport == "streamable-http"
