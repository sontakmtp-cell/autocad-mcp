from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from autocad_mcp.backends.file_ipc import (
    DocumentSnapshot,
    FileIPCBackend,
    RuntimeState,
    encode_attributes,
)
from autocad_mcp.backends.base import CommandResult


class FakeDocument:
    def __init__(self, callback=None, *, name="a.dwg", full_name="C:/a.dwg", hwnd=22):
        self.Name = name
        self.FullName = full_name
        self.HWND = hwnd
        self.callback = callback
        self.commands: list[str] = []

    def PostCommand(self, command: str):
        self.commands.append(command)
        if self.callback:
            self.callback(command)

    def SendCommand(self, command: str):
        self.commands.append(command)
        if self.callback:
            self.callback(command)

    def GetVariable(self, name: str):
        assert name == "CMDACTIVE"
        return 0


class FakeState:
    IsQuiescent = True


class FakeApp:
    def __init__(self, document):
        self.ActiveDocument = document
        self.HWND = 11

    def GetAcadState(self):
        return FakeState()


def snapshot(name="a.dwg", full_name="C:/a.dwg", doc_hwnd=22):
    return DocumentSnapshot(name, full_name, f"11:{doc_hwnd}:{full_name}", 11, doc_hwnd)


def runtime(document=None, snap=None, *, idle=True, modal=False, cmdactive=0):
    document = document or FakeDocument()
    snap = snap or snapshot(document.Name, document.FullName, document.HWND)
    return RuntimeState(FakeApp(document), document, snap, True, idle, cmdactive, modal)


def failing_state(code, message):
    return RuntimeState(None, None, None, False, None, None, False, code, message)


def install_result_callback(backend: FileIPCBackend, doc: FakeDocument, payload=None, *, request_id_override=None):
    def callback(command: str):
        match = re.search(r'c:mcp-dispatch-request\s+"([0-9a-f]+)"\s+"([0-9a-f]+)"', command)
        assert match, command
        session_id, request_id = match.groups()
        _, result_file, _ = backend._paths(request_id)
        result_file.write_text(
            json.dumps({
                "request_id": request_id_override or request_id,
                "session_id": session_id,
                "ok": True,
                "payload": payload if payload is not None else "pong",
            }),
            encoding="utf-8",
        )
    doc.callback = callback


@pytest.fixture
def backend(tmp_path, monkeypatch):
    instance = FileIPCBackend()
    instance._ipc_dir = tmp_path
    monkeypatch.setattr("autocad_mcp.backends.file_ipc.TIMEOUT", 0.05)
    monkeypatch.setattr("autocad_mcp.backends.file_ipc.POLL_INTERVAL", 0.005)
    monkeypatch.setattr("autocad_mcp.backends.file_ipc.IDLE_WAIT_TIMEOUT", 0.01)
    return instance


@pytest.mark.asyncio
async def test_autocad_window_not_found(backend, monkeypatch):
    monkeypatch.setattr(backend, "_inspect_runtime", lambda: failing_state("autocad_not_running", "not running"))
    result = await backend.initialize()
    assert result.ok is False
    assert result.error_code == "autocad_not_running"


@pytest.mark.asyncio
async def test_no_active_document(backend, monkeypatch):
    monkeypatch.setattr(backend, "_inspect_runtime", lambda: failing_state("no_active_document", "none"))
    result = await backend.initialize()
    assert result.error_code == "no_active_document"


@pytest.mark.asyncio
async def test_dispatcher_ping_success(backend, monkeypatch):
    doc = FakeDocument()
    install_result_callback(backend, doc)
    state = runtime(doc)
    monkeypatch.setattr(backend, "_inspect_runtime", lambda: state)
    result = await backend._dispatch("ping", {}, retry_ping=True)
    assert result.ok is True
    assert result.payload == "pong"
    assert backend._last_routing_method == "PostCommand"


@pytest.mark.asyncio
async def test_timeout_but_autocad_busy_is_not_not_loaded(backend, monkeypatch):
    doc = FakeDocument()
    states = iter([runtime(doc), runtime(doc, idle=False, cmdactive=1)])
    monkeypatch.setattr(backend, "_inspect_runtime", lambda: next(states))
    result = await backend._dispatch_once("ping", {})
    assert result.error_code == "autocad_busy"
    assert "not loaded" not in (result.error or "").lower()


@pytest.mark.asyncio
async def test_timeout_after_active_document_change(backend, monkeypatch):
    doc_a = FakeDocument(name="a.dwg", full_name="C:/a.dwg", hwnd=22)
    doc_b = FakeDocument(name="b.dwg", full_name="C:/b.dwg", hwnd=23)
    states = iter([runtime(doc_a), runtime(doc_b, snapshot("b.dwg", "C:/b.dwg", 23))])
    monkeypatch.setattr(backend, "_inspect_runtime", lambda: next(states))
    result = await backend._dispatch_once("ping", {})
    assert result.error_code == "active_document_changed"
    assert result.details["active_document"] == "b.dwg"


@pytest.mark.asyncio
async def test_active_document_changes_between_commands(backend, monkeypatch):
    doc_a = FakeDocument(name="a.dwg", full_name="C:/a.dwg", hwnd=22)
    doc_b = FakeDocument(name="b.dwg", full_name="C:/b.dwg", hwnd=23)
    install_result_callback(backend, doc_a, {"doc": "a"})
    install_result_callback(backend, doc_b, {"doc": "b"})
    current = {"state": runtime(doc_a)}
    monkeypatch.setattr(backend, "_inspect_runtime", lambda: current["state"])
    first = await backend.drawing_info()
    current["state"] = runtime(doc_b, snapshot("b.dwg", "C:/b.dwg", 23))
    second = await backend.drawing_info()
    assert first.payload == {"doc": "a"}
    assert second.payload == {"doc": "b"}
    assert backend._last_document.name == "b.dwg"


@pytest.mark.asyncio
async def test_health_calls_real_ping(backend, monkeypatch):
    doc = FakeDocument()
    monkeypatch.setattr(backend, "_inspect_runtime", lambda: runtime(doc))
    dispatch = AsyncMock(return_value=CommandResult(True, payload="pong"))
    monkeypatch.setattr(backend, "_dispatch", dispatch)
    result = await backend.health()
    assert result.ok is True
    assert result.payload["dispatcher_reachable"] is True
    dispatch.assert_awaited_once_with("ping", {}, retry_ping=True)


@pytest.mark.asyncio
async def test_plain_timeout_is_dispatcher_timeout_not_missing(backend, monkeypatch):
    doc = FakeDocument()
    monkeypatch.setattr(backend, "_inspect_runtime", lambda: runtime(doc))
    result = await backend._dispatch_once("drawing-info", {})
    assert result.error_code == "dispatcher_timeout"
    assert "not loaded" not in (result.error or "").lower()


@pytest.mark.asyncio
async def test_write_command_is_not_retried(backend, monkeypatch):
    dispatch_once = AsyncMock(return_value=CommandResult(False, error="timeout", error_code="dispatcher_timeout"))
    monkeypatch.setattr(backend, "_dispatch_once", dispatch_once)
    result = await backend.block_insert("B", 0, 0)
    assert result.ok is False
    assert dispatch_once.await_count == 1


@pytest.mark.asyncio
async def test_ping_retries_at_most_once(backend, monkeypatch):
    dispatch_once = AsyncMock(side_effect=[
        CommandResult(False, error="timeout", error_code="dispatcher_timeout"),
        CommandResult(True, payload="pong"),
    ])
    monkeypatch.setattr(backend, "_dispatch_once", dispatch_once)
    monkeypatch.setattr(backend, "_inspect_runtime", lambda: runtime())
    result = await backend._dispatch("ping", {}, retry_ping=True)
    assert result.ok is True
    assert dispatch_once.await_count == 2


@pytest.mark.asyncio
async def test_com_command_routing_success(backend, monkeypatch):
    doc = FakeDocument()
    install_result_callback(backend, doc)
    monkeypatch.setattr(backend, "_inspect_runtime", lambda: runtime(doc))
    result = await backend.drawing_info()
    assert result.ok is True
    assert len(doc.commands) == 1
    assert "mcp-dispatch-request" in doc.commands[0]


def test_com_unavailable_is_explicit(monkeypatch):
    backend = FileIPCBackend()
    monkeypatch.setattr(backend, "_connect_com", lambda: (None, "COM unavailable"))
    monkeypatch.setattr("autocad_mcp.backends.file_ipc.find_autocad_window", lambda: 123)
    state = backend._inspect_runtime()
    assert state.error_code == "command_routing_failed"
    assert "COM unavailable" in state.error


def test_missing_dispatcher_probe_uses_defined_symbol_check(backend):
    expression = backend._trigger_expression(
        backend._ipc_dir / "result.json",
        "deadbeef0000",
    )
    assert 'atoms-family 1 \'("C:MCP-DISPATCH-REQUEST")' in expression
    assert "(if c:mcp-dispatch-request" not in expression


@pytest.mark.asyncio
async def test_result_request_id_mismatch(backend, monkeypatch):
    doc = FakeDocument()
    install_result_callback(backend, doc, request_id_override="deadbeef0000")
    monkeypatch.setattr(backend, "_inspect_runtime", lambda: runtime(doc))
    result = await backend.drawing_info()
    assert result.error_code == "ipc_result_invalid"


@pytest.mark.asyncio
async def test_stale_result_file_is_not_used(backend, monkeypatch):
    stale = backend._ipc_dir / "autocad_mcp_result_oldsession_oldrequest.json"
    stale.write_text(json.dumps({"ok": True, "payload": "stale"}), encoding="utf-8")
    doc = FakeDocument()
    monkeypatch.setattr(backend, "_inspect_runtime", lambda: runtime(doc))
    result = await backend.drawing_info()
    assert result.error_code == "dispatcher_timeout"
    assert stale.exists()


@pytest.mark.asyncio
async def test_two_requests_are_serialized(backend, monkeypatch):
    active = 0
    maximum = 0

    async def fake_once(command, params):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02)
        active -= 1
        return CommandResult(True, payload=command)

    monkeypatch.setattr(backend, "_dispatch_once", fake_once)
    await asyncio.gather(backend.drawing_info(), backend.layer_list())
    assert maximum == 1


@pytest.mark.asyncio
async def test_modal_dialog_is_reported_without_routing(backend, monkeypatch):
    doc = FakeDocument()
    monkeypatch.setattr(backend, "_inspect_runtime", lambda: runtime(doc, idle=False, modal=True, cmdactive=8))
    result = await backend.drawing_info()
    assert result.error_code == "modal_dialog_active"
    assert doc.commands == []


def test_attributes_encoding_spaces_and_unicode():
    encoded = encode_attributes({"TAG": "Máy bơm số 1", "NOTE": "hello world"})
    assert encoded == "3:TAG12:Máy bơm số 14:NOTE11:hello world"


def test_command_result_error_model():
    result = CommandResult(False, error="busy", error_code="autocad_busy", details={"cmdactive": 1})
    assert result.to_dict() == {
        "ok": False,
        "error": "busy",
        "error_code": "autocad_busy",
        "details": {"cmdactive": 1},
    }

@pytest.mark.asyncio
async def test_drawing_open_uses_activex_and_tracks_new_document(backend, monkeypatch):
    old_doc = FakeDocument(name="a.dwg", full_name="C:/a.dwg", hwnd=22)
    new_doc = FakeDocument(name="b.dwg", full_name="C:/b.dwg", hwnd=23)
    app = FakeApp(old_doc)

    class Documents:
        def Open(self, path):
            assert path == "C:/b.dwg"
            app.ActiveDocument = new_doc
            return new_doc

    app.Documents = Documents()
    state = runtime(old_doc)
    state.app = app
    monkeypatch.setattr(backend, "_inspect_runtime", lambda: state)
    result = await backend.drawing_open("C:/b.dwg")
    assert result.ok is True
    assert result.payload["active_document"] == "b.dwg"
    assert backend._last_document.name == "b.dwg"
    assert old_doc.commands == []
