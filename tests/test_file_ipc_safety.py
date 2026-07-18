"""Regression tests for fail-closed File IPC dispatch and command recovery."""

from __future__ import annotations

import json

import pytest

from autocad_mcp import client
from autocad_mcp.backends.base import CommandResult
from autocad_mcp.backends.file_ipc import FileIPCBackend
from autocad_mcp.backends.safe_file_ipc import SafeFileIPCBackend
from autocad_mcp.config import TransportConfig


def _remote_dev_config() -> TransportConfig:
    return TransportConfig(
        transport="streamable-http",
        host="127.0.0.1",
        port=8765,
        path="/mcp",
        remote_profile="dev",
        allow_no_auth=True,
    )


def test_missing_dispatcher_error_keeps_specific_code():
    payload = json.loads(
        client._error(RuntimeError("dispatcher_missing_in_active_document"))
    )
    assert payload["error_code"] == "dispatcher_missing_in_active_document"


@pytest.mark.asyncio
async def test_execute_lisp_positional_call_is_denied_before_handler(monkeypatch):
    called = False

    monkeypatch.setattr(client, "_current_transport_config", _remote_dev_config)
    monkeypatch.setattr(client, "get_access_token", lambda: None)
    monkeypatch.setattr(client, "_backend", None)

    @client._safe("system")
    async def handler(operation: str, data: dict | None = None):
        nonlocal called
        called = True
        return json.dumps({"ok": True})

    result = await handler("execute_lisp", {"code": "(+ 1 2)"})
    payload = json.loads(result)

    assert called is False
    assert payload["ok"] is False
    assert payload["code"] == "execute_lisp_denied"


@pytest.mark.asyncio
async def test_remote_execute_lisp_creates_no_ipc_or_lisp_file(tmp_path, monkeypatch):
    backend = SafeFileIPCBackend(allow_execute_lisp=False)
    backend._ipc_dir = tmp_path

    result = await backend.execute_lisp("(+ 1 2)")

    assert result.ok is False
    assert "disabled" in result.error
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_unsupported_command_never_calls_dispatch(monkeypatch):
    async def dispatch(*args, **kwargs):
        raise AssertionError("dispatch must not run")

    monkeypatch.setattr(FileIPCBackend, "_dispatch", dispatch)

    backend = SafeFileIPCBackend()
    result = await backend._dispatch("ATTDIA", {})

    assert result.ok is False
    assert "Unsupported File IPC command" in result.error
    assert result.error_code == "unsupported_operation"


@pytest.mark.asyncio
async def test_failed_dispatch_is_returned_without_user_command_recovery(monkeypatch):
    async def failed_dispatch(self, command, params, retry_ping=False):
        return CommandResult(ok=False, error="Timeout waiting for result")

    monkeypatch.setattr(FileIPCBackend, "_dispatch", failed_dispatch)

    backend = SafeFileIPCBackend()
    result = await backend._dispatch("ping", {})

    assert result.ok is False
    assert result.error == "Timeout waiting for result"


@pytest.mark.asyncio
async def test_dispatch_exception_returns_structured_error(monkeypatch):
    async def broken_dispatch(self, command, params, retry_ping=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(FileIPCBackend, "_dispatch", broken_dispatch)

    backend = SafeFileIPCBackend()
    result = await backend._dispatch("ping", {})

    assert result.to_dict() == {
        "ok": False,
        "error": "File IPC dispatch failed: boom",
        "error_code": "command_routing_failed",
    }
