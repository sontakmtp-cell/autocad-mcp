"""Reliable file-based IPC backend for AutoCAD LT 2024+.

The backend routes a fixed dispatcher expression through AutoCAD ActiveX/COM.
It never focuses the AutoCAD window, simulates keystrokes, opens APPLOAD, or
sends ESC to cancel a user's command.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import structlog
except ImportError:  # pragma: no cover - test environments may not install logging deps
    class _Logger:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None
    class _Structlog:
        @staticmethod
        def get_logger():
            return _Logger()
    structlog = _Structlog()  # type: ignore[assignment]

from autocad_mcp.backends.base import AutoCADBackend, BackendCapabilities, CommandResult
from autocad_mcp.config import IPC_DIR, IPC_TIMEOUT

log = structlog.get_logger()

POLL_INTERVAL = 0.1
TIMEOUT = IPC_TIMEOUT
STALE_THRESHOLD = 60.0
IDLE_WAIT_TIMEOUT = min(2.0, max(0.25, TIMEOUT / 4.0))
PING_RETRY_LIMIT = 1
_SESSION_RE = re.compile(r"^[0-9a-f]{16}$")
_REQUEST_RE = re.compile(r"^[0-9a-f]{12}$")


@dataclass(frozen=True)
class DocumentSnapshot:
    name: str
    full_name: str
    identity: str
    app_hwnd: int | None
    document_hwnd: int | None


@dataclass
class RuntimeState:
    app: Any | None
    document: Any | None
    snapshot: DocumentSnapshot | None
    window_found: bool
    idle: bool | None
    cmdactive: int | None
    modal_dialog_active: bool
    error_code: str | None = None
    error: str | None = None


def _error(code: str, message: str, **details: Any) -> CommandResult:
    return CommandResult(False, error=message, error_code=code, details=details or None)


def find_autocad_window() -> int | None:
    """Find a visible AutoCAD top-level window without changing focus."""
    if sys.platform != "win32":
        return None
    try:
        import win32gui
    except ImportError:
        return None

    windows: list[int] = []

    def callback(hwnd, result):
        if win32gui.IsWindowVisible(hwnd):
            text = win32gui.GetWindowText(hwnd).lower()
            if "autocad" in text:
                result.append(hwnd)
        return True

    try:
        win32gui.EnumWindows(callback, windows)
    except Exception:
        return None
    return windows[0] if windows else None


def encode_attributes(attributes: dict[str, Any] | None) -> str:
    """Encode tag/value pairs using a delimiter-free length-prefixed format.

    Format: ``<tag_len>:<tag><value_len>:<value>`` repeated for each pair.
    Spaces, punctuation, and Unicode are preserved without nested JSON parsing.
    """
    if not attributes:
        return ""
    parts: list[str] = []
    for raw_tag, raw_value in attributes.items():
        tag = str(raw_tag)
        value = str(raw_value)
        if not tag:
            raise ValueError("Attribute tag must not be empty")
        parts.append(f"{len(tag)}:{tag}{len(value)}:{value}")
    return "".join(parts)


def _lisp_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _lisp_path(value: str) -> str:
    """Use separators that survive the dispatcher's minimal JSON parser."""

    return value.replace("\\", "/")


class FileIPCBackend(AutoCADBackend):
    """File IPC with exact request routing through ActiveX/COM."""

    def __init__(self):
        self._hwnd: int | None = None
        self._ipc_dir = Path(IPC_DIR)
        self._screenshot_provider = None
        self._lock = asyncio.Lock()
        self._session_id = uuid.uuid4().hex[:16]
        self._last_document: DocumentSnapshot | None = None
        self._last_routing_method: str | None = None

    @property
    def name(self) -> str:
        return "file_ipc"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            can_read_drawing=True,
            can_modify_entities=True,
            can_create_entities=True,
            can_screenshot=True,
            can_save=True,
            can_plot_pdf=True,
            can_zoom=True,
            can_query_entities=True,
            can_file_operations=True,
            can_undo=True,
        )

    async def initialize(self) -> CommandResult:
        """Initialize routing without turning ping failures into "LISP not loaded"."""
        self._ipc_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_files()

        state = self._inspect_runtime()
        if state.error_code:
            return _error(state.error_code, state.error or state.error_code)
        assert state.snapshot is not None
        self._hwnd = state.snapshot.app_hwnd or find_autocad_window()
        self._last_document = state.snapshot

        try:
            from autocad_mcp.screenshot import Win32ScreenshotProvider
            if self._hwnd:
                self._screenshot_provider = Win32ScreenshotProvider(self._hwnd)
        except Exception:
            self._screenshot_provider = None

        # A real probe is recorded but is not rewritten as a generic load error.
        probe = await self._dispatch("ping", {}, retry_ping=True)
        return CommandResult(
            True,
            payload={
                "backend": self.name,
                "hwnd": self._hwnd,
                "active_document": state.snapshot.name,
                "dispatcher_probe": probe.to_dict(),
            },
        )

    async def status(self) -> CommandResult:
        return CommandResult(
            True,
            payload={
                "backend": self.name,
                "hwnd": self._hwnd,
                "ipc_dir": str(self._ipc_dir),
                "session_id": self._session_id,
                "active_document": self._last_document.name if self._last_document else None,
                "capabilities": dict(self.capabilities.__dict__),
            },
        )

    async def health(self) -> CommandResult:
        started = time.perf_counter()
        state = self._inspect_runtime()
        base = {
            "backend": self.name,
            "autocad_window_found": state.window_found,
            "active_document": state.snapshot.name if state.snapshot else None,
            "autocad_idle": state.idle,
            "modal_dialog_active": state.modal_dialog_active,
            "dispatcher_reachable": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        if state.error_code:
            return _error(state.error_code, state.error or state.error_code, **base)
        if state.modal_dialog_active:
            return _error("modal_dialog_active", "AutoCAD has a modal dialog open.", **base)
        if state.idle is False:
            return _error("autocad_busy", "AutoCAD is running another command.", **base)

        result = await self._dispatch("ping", {}, retry_ping=True)
        base["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        base["dispatcher_reachable"] = result.ok
        if result.ok:
            return CommandResult(True, payload={"ok": True, **base})
        details = dict(base)
        if result.details:
            details.update(result.details)
        return _error(
            result.error_code or "dispatcher_timeout",
            result.error or "Dispatcher health probe failed.",
            **details,
        )

    def _candidate_progids(self) -> list[str]:
        candidates: list[str] = []
        explicit = os.environ.get("AUTOCAD_MCP_COM_PROGID", "").strip()
        if explicit:
            candidates.append(explicit)
        if sys.platform == "win32":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"AutoCAD.Application\CurVer") as key:
                    current, _ = winreg.QueryValueEx(key, None)
                    if current:
                        candidates.append(str(current))
            except Exception:
                pass
        # Version-independent ProgID is a final compatibility lookup only; it
        # never starts a new AutoCAD process because GetActiveObject is used.
        candidates.append("AutoCAD.Application")
        return list(dict.fromkeys(candidates))

    def _connect_com(self) -> tuple[Any | None, str | None]:
        if sys.platform != "win32":
            return None, "ActiveX/COM routing requires native Windows Python."
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
        except ImportError as exc:
            return None, f"pywin32 COM support is unavailable: {exc}"

        errors: list[str] = []
        for progid in self._candidate_progids():
            try:
                return win32com.client.GetActiveObject(progid), None
            except Exception as exc:
                errors.append(f"{progid}: {exc}")
        return None, "; ".join(errors) or "No running AutoCAD COM object was found."

    @staticmethod
    def _snapshot(app: Any, document: Any) -> DocumentSnapshot:
        name = str(getattr(document, "Name", "") or "")
        full_name = str(getattr(document, "FullName", "") or "")
        try:
            app_hwnd = int(getattr(app, "HWND"))
        except Exception:
            app_hwnd = None
        try:
            document_hwnd = int(getattr(document, "HWND"))
        except Exception:
            document_hwnd = None
        identity = f"{app_hwnd or 0}:{document_hwnd or 0}:{full_name or name}"
        return DocumentSnapshot(name, full_name, identity, app_hwnd, document_hwnd)

    def _inspect_runtime(self) -> RuntimeState:
        app, com_error = self._connect_com()
        window = find_autocad_window()
        if app is None:
            if not window:
                return RuntimeState(None, None, None, False, None, None, False,
                                    "autocad_not_running", "AutoCAD LT is not running.")
            return RuntimeState(None, None, None, True, None, None, False,
                                "command_routing_failed", com_error or "COM routing failed.")

        try:
            app_hwnd = int(getattr(app, "HWND"))
        except Exception:
            app_hwnd = window
        window_found = bool(app_hwnd or window)

        try:
            document = app.ActiveDocument
        except Exception as exc:
            return RuntimeState(app, None, None, window_found, None, None, False,
                                "no_active_document", f"AutoCAD has no active document: {exc}")
        if document is None:
            return RuntimeState(app, None, None, window_found, None, None, False,
                                "no_active_document", "AutoCAD has no active document.")

        snapshot = self._snapshot(app, document)
        cmdactive: int | None = None
        try:
            cmdactive = int(document.GetVariable("CMDACTIVE"))
        except Exception:
            pass
        modal = bool(cmdactive is not None and (cmdactive & 8))
        try:
            idle = bool(app.GetAcadState().IsQuiescent)
            if cmdactive not in (None, 0):
                idle = False
        except Exception as exc:
            return RuntimeState(app, document, snapshot, window_found, None, cmdactive, modal,
                                "command_routing_failed", f"Cannot inspect AutoCAD state: {exc}")
        return RuntimeState(app, document, snapshot, window_found, idle, cmdactive, modal)

    async def _wait_until_idle(self) -> RuntimeState:
        deadline = time.monotonic() + IDLE_WAIT_TIMEOUT
        last = self._inspect_runtime()
        while not last.error_code and not last.modal_dialog_active and last.idle is False:
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(POLL_INTERVAL)
            last = self._inspect_runtime()
        return last

    def _paths(self, request_id: str) -> tuple[Path, Path, Path]:
        stem = f"{self._session_id}_{request_id}"
        cmd = self._ipc_dir / f"autocad_mcp_cmd_{stem}.json"
        result = self._ipc_dir / f"autocad_mcp_result_{stem}.json"
        return cmd, result, cmd.with_suffix(".tmp")

    def _missing_dispatcher_expression(self, result_file: Path, request_id: str) -> str:
        final_path = str(result_file).replace("\\", "/")
        tmp_path = final_path + ".tmp"
        payload = json.dumps(
            {
                "request_id": request_id,
                "session_id": self._session_id,
                "ok": False,
                "error_code": "dispatcher_missing_in_active_document",
                "error": "mcp_dispatch.lsp is not loaded in the active document",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (
            "(progn "
            f'(setq mcp-f (open "{_lisp_string(tmp_path)}" "w")) '
            f'(if mcp-f (progn (write-line "{_lisp_string(payload)}" mcp-f) '
            f'(close mcp-f) (vl-file-rename "{_lisp_string(tmp_path)}" '
            f'"{_lisp_string(final_path)}"))))'
        )

    def _trigger_expression(self, result_file: Path, request_id: str) -> str:
        missing = self._missing_dispatcher_expression(result_file, request_id)
        return (
            '(if (car (atoms-family 1 \'("C:MCP-DISPATCH-REQUEST"))) '
            f'(c:mcp-dispatch-request "{self._session_id}" "{request_id}") '
            f"{missing})"
        )

    def _route(self, document: Any, expression: str) -> CommandResult:
        command = expression + "\r"
        try:
            post = getattr(document, "PostCommand")
            post(command)
            self._last_routing_method = "PostCommand"
            return CommandResult(True, payload={"routing_method": "PostCommand"})
        except Exception as post_exc:
            try:
                send = getattr(document, "SendCommand")
                send(command)
                self._last_routing_method = "SendCommand"
                log.warning("post_command_unavailable_using_send_command", error=str(post_exc))
                return CommandResult(True, payload={"routing_method": "SendCommand"})
            except Exception as send_exc:
                return _error(
                    "command_routing_failed",
                    "AutoCAD ActiveX command routing failed.",
                    post_error=str(post_exc),
                    send_error=str(send_exc),
                )

    async def _dispatch(self, command: str, params: dict, retry_ping: bool = False) -> CommandResult:
        async with self._lock:
            attempts = 1 + (PING_RETRY_LIMIT if command == "ping" and retry_ping else 0)
            result: CommandResult | None = None
            for attempt in range(attempts):
                result = await self._dispatch_once(command, params)
                if result.ok:
                    return result
                if command != "ping" or attempt + 1 >= attempts:
                    return result
                if result.error_code not in {"dispatcher_timeout", "active_document_changed"}:
                    return result
                state = self._inspect_runtime()
                if state.error_code or state.modal_dialog_active or state.idle is False:
                    return result
            assert result is not None
            return result

    async def _dispatch_once(self, command: str, params: dict) -> CommandResult:
        state = await self._wait_until_idle()
        if state.error_code:
            return _error(state.error_code, state.error or state.error_code)
        if state.modal_dialog_active:
            return _error("modal_dialog_active", "AutoCAD has a modal dialog open.")
        if state.idle is False:
            return _error("autocad_busy", "AutoCAD is running another command; MCP did not send ESC.",
                          cmdactive=state.cmdactive)
        if state.document is None or state.snapshot is None:
            return _error("no_active_document", "AutoCAD has no active document.")

        request_id = uuid.uuid4().hex[:12]
        if not _SESSION_RE.match(self._session_id) or not _REQUEST_RE.match(request_id):
            return _error("ipc_result_invalid", "Generated IPC identifiers are invalid.")
        cmd_file, result_file, tmp_file = self._paths(request_id)
        expected_document = state.snapshot
        clean_params = {key: value for key, value in params.items() if value is not None}
        payload = {
            "request_id": request_id,
            "session_id": self._session_id,
            "command": command,
            "params": clean_params,
            "document_identity": expected_document.identity,
            "ts": time.time(),
        }
        request_started = time.perf_counter()
        log.info(
            "ipc_request_started",
            request_id=request_id,
            session_id=self._session_id,
            command=command,
        )

        try:
            tmp_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_file, cmd_file)
            routed = self._route(state.document, self._trigger_expression(result_file, request_id))
            if not routed.ok:
                return routed

            deadline = time.monotonic() + TIMEOUT
            while time.monotonic() < deadline:
                if result_file.exists():
                    parsed = self._read_result(result_file)
                    if isinstance(parsed, CommandResult):
                        return parsed
                    if parsed.get("request_id") != request_id or parsed.get("session_id") != self._session_id:
                        return _error(
                            "ipc_result_invalid",
                            "IPC result identifiers do not match the request.",
                            expected_request_id=request_id,
                            actual_request_id=parsed.get("request_id"),
                        )
                    self._last_document = expected_document
                    result = CommandResult(
                        bool(parsed.get("ok", False)),
                        payload=parsed.get("payload"),
                        error=parsed.get("error"),
                        error_code=parsed.get("error_code"),
                        details=parsed.get("details"),
                    )
                    log.info(
                        "ipc_request_finished",
                        request_id=request_id,
                        session_id=self._session_id,
                        command=command,
                        ok=result.ok,
                        error_code=result.error_code,
                        latency_ms=round((time.perf_counter() - request_started) * 1000, 2),
                    )
                    return result
                await asyncio.sleep(POLL_INTERVAL)

            current = self._inspect_runtime()
            if current.error_code:
                return _error(current.error_code, current.error or current.error_code)
            if current.modal_dialog_active:
                return _error("modal_dialog_active", "AutoCAD entered a modal dialog while waiting for IPC.")
            if current.idle is False:
                return _error("autocad_busy", "AutoCAD is busy after command routing.", cmdactive=current.cmdactive)
            if current.snapshot and current.snapshot.identity != expected_document.identity:
                return _error(
                    "active_document_changed",
                    "The active AutoCAD document changed while the request was running.",
                    previous_document=expected_document.name,
                    active_document=current.snapshot.name,
                )
            return _error(
                "dispatcher_timeout",
                "Dispatcher did not return an IPC result before timeout.",
                request_id=request_id,
                active_document=expected_document.name,
                routing_method=self._last_routing_method,
            )
        finally:
            for path in (cmd_file, result_file, tmp_file):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _read_result(result_file: Path) -> dict[str, Any] | CommandResult:
        try:
            try:
                text = result_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = result_file.read_text(encoding="cp1252")
            data = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            return _error("ipc_result_invalid", f"IPC result is not valid JSON: {exc}")
        if not isinstance(data, dict):
            return _error("ipc_result_invalid", "IPC result must be a JSON object.")
        return data

    def _cleanup_stale_files(self) -> None:
        """Delete only old IPC artifacts; never remove a fresh in-flight request."""
        try:
            now = time.time()
            for pattern in ("autocad_mcp_cmd_*.json", "autocad_mcp_result_*.json", "autocad_mcp_*.tmp", "autocad_mcp_lisp_*.lsp"):
                for path in self._ipc_dir.glob(pattern):
                    try:
                        if now - path.stat().st_mtime > STALE_THRESHOLD:
                            path.unlink(missing_ok=True)
                    except OSError:
                        continue
        except OSError:
            pass

    # Drawing management
    async def drawing_info(self): return await self._dispatch("drawing-info", {})
    async def drawing_save(self, path=None): return await self._dispatch("drawing-save", {"path": path})
    async def drawing_save_as_dxf(self, path): return await self._dispatch("drawing-save-as-dxf", {"path": path})
    async def drawing_create(self, name=None): return await self._dispatch("drawing-create", {"name": name})
    async def drawing_purge(self): return await self._dispatch("drawing-purge", {})
    async def drawing_plot_pdf(self, path): return await self._dispatch("drawing-plot-pdf", {"path": path})
    async def drawing_get_variables(self, names=None):
        names_str = ";".join(name.lstrip("$") for name in names) if names else ""
        return await self._dispatch("drawing-get-variables", {"names_str": names_str})
    async def drawing_open(self, path):
        """Open a DWG through the official ActiveX Documents.Open API.

        Opening a document through the LISP command would switch namespaces
        before the old dispatcher can reliably write its result.
        """
        async with self._lock:
            state = await self._wait_until_idle()
            if state.error_code:
                return _error(state.error_code, state.error or state.error_code)
            if state.modal_dialog_active:
                return _error("modal_dialog_active", "AutoCAD has a modal dialog open.")
            if state.idle is False:
                return _error("autocad_busy", "AutoCAD is running another command; MCP did not send ESC.")
            try:
                opened = state.app.Documents.Open(path)
                active = state.app.ActiveDocument or opened
                self._last_document = self._snapshot(state.app, active)
                return CommandResult(True, payload={
                    "opened": self._last_document.name,
                    "active_document": self._last_document.name,
                })
            except Exception as exc:
                return _error("command_routing_failed", f"ActiveX Documents.Open failed: {exc}")
    async def undo(self): return await self._dispatch("undo", {})
    async def redo(self): return await self._dispatch("redo", {})

    async def execute_lisp(self, code: str):
        request_id = uuid.uuid4().hex[:12]
        code_file = self._ipc_dir / f"autocad_mcp_lisp_{self._session_id}_{request_id}.lsp"
        code_file.write_text(code, encoding="utf-8")
        return await self._dispatch("execute-lisp", {"code_file": str(code_file).replace("\\", "/")})

    # Entity operations
    async def create_line(self, x1, y1, x2, y2, layer=None): return await self._dispatch("create-line", locals_without_self(locals()))
    async def create_circle(self, cx, cy, radius, layer=None): return await self._dispatch("create-circle", locals_without_self(locals()))
    async def create_polyline(self, points, closed=False, layer=None):
        return await self._dispatch("create-polyline", {"points_str": ";".join(f"{p[0]},{p[1]}" for p in points), "closed": "1" if closed else "0", "layer": layer})
    async def create_rectangle(self, x1, y1, x2, y2, layer=None): return await self._dispatch("create-rectangle", locals_without_self(locals()))
    async def create_arc(self, cx, cy, radius, start_angle, end_angle, layer=None): return await self._dispatch("create-arc", locals_without_self(locals()))
    async def create_ellipse(self, cx, cy, major_x, major_y, ratio, layer=None): return await self._dispatch("create-ellipse", locals_without_self(locals()))
    async def create_mtext(self, x, y, width, text, height=2.5, layer=None): return await self._dispatch("create-mtext", locals_without_self(locals()))
    async def create_hatch(self, entity_id, pattern="ANSI31"): return await self._dispatch("create-hatch", locals_without_self(locals()))
    async def entity_list(self, layer=None): return await self._dispatch("entity-list", {"layer": layer})
    async def entity_count(self, layer=None): return await self._dispatch("entity-count", {"layer": layer})
    async def entity_get(self, entity_id): return await self._dispatch("entity-get", {"entity_id": entity_id})
    async def entity_erase(self, entity_id): return await self._dispatch("entity-erase", {"entity_id": entity_id})
    async def entity_copy(self, entity_id, dx, dy): return await self._dispatch("entity-copy", locals_without_self(locals()))
    async def entity_move(self, entity_id, dx, dy): return await self._dispatch("entity-move", locals_without_self(locals()))
    async def entity_rotate(self, entity_id, cx, cy, angle): return await self._dispatch("entity-rotate", locals_without_self(locals()))
    async def entity_scale(self, entity_id, cx, cy, factor): return await self._dispatch("entity-scale", locals_without_self(locals()))
    async def entity_mirror(self, entity_id, x1, y1, x2, y2): return await self._dispatch("entity-mirror", locals_without_self(locals()))
    async def entity_offset(self, entity_id, distance): return await self._dispatch("entity-offset", locals_without_self(locals()))
    async def entity_array(self, entity_id, rows, cols, row_dist, col_dist): return await self._dispatch("entity-array", locals_without_self(locals()))
    async def entity_fillet(self, entity_id1, entity_id2, radius): return await self._dispatch("entity-fillet", {"id1": entity_id1, "id2": entity_id2, "radius": radius})
    async def entity_chamfer(self, entity_id1, entity_id2, dist1, dist2): return await self._dispatch("entity-chamfer", {"id1": entity_id1, "id2": entity_id2, "dist1": dist1, "dist2": dist2})

    async def annotation_export_dimension_geometry(
        self,
        *,
        lisp_path: str,
        report_path: str,
        dimension_layer: str,
        source_layers: str = "",
        use_current_selection: bool = False,
    ) -> CommandResult:
        """Run the fixed, read-only annotation exporter through safe IPC."""

        params = {
            "lisp_path": _lisp_path(lisp_path),
            "report_path": _lisp_path(report_path),
            "dimension_layer": dimension_layer,
            "use_current_selection": "1" if use_current_selection else "0",
        }
        if source_layers:
            params["source_layers"] = source_layers
        return await self._dispatch(
            "annotation-export-dimension-geometry",
            params,
        )

    async def annotation_commit_dimension_plan(
        self,
        *,
        lisp_path: str,
        plan_path: str,
        report_path: str,
        dimension_layer: str,
        dimstyle: str,
        scale_factor: float,
        clear_existing: bool,
        text_height: float,
        arrow_size: float,
        precision: int,
        tolerance_mode: str,
        tolerance_upper: float,
        tolerance_lower: float,
    ) -> CommandResult:
        """Commit one validated plan through a dedicated single-UNDO command."""

        return await self._dispatch(
            "annotation-commit-dimension-plan",
            {
                "lisp_path": _lisp_path(lisp_path),
                "plan_path": _lisp_path(plan_path),
                "report_path": _lisp_path(report_path),
                "dimension_layer": dimension_layer,
                "dimstyle": dimstyle,
                "scale_factor": scale_factor,
                "clear_existing": "1" if clear_existing else "0",
                "text_height": text_height,
                "arrow_size": arrow_size,
                "precision": precision,
                "tolerance_mode": tolerance_mode,
                "tolerance_upper": tolerance_upper,
                "tolerance_lower": tolerance_lower,
            },
        )

    async def annotation_repair_dimensions(
        self,
        *,
        lisp_path: str,
        actions_path: str,
        report_path: str,
        dimension_layer: str,
        dimstyle: str,
    ) -> CommandResult:
        """Apply a server-audited repair batch through one safe IPC command."""

        return await self._dispatch(
            "annotation-repair-dimensions",
            {
                "lisp_path": _lisp_path(lisp_path),
                "actions_path": _lisp_path(actions_path),
                "report_path": _lisp_path(report_path),
                "dimension_layer": dimension_layer,
                "dimstyle": dimstyle,
            },
        )

    # Layer operations
    async def layer_list(self): return await self._dispatch("layer-list", {})
    async def layer_create(self, name, color="white", linetype="CONTINUOUS"): return await self._dispatch("layer-create", locals_without_self(locals()))
    async def layer_set_current(self, name): return await self._dispatch("layer-set-current", {"name": name})
    async def layer_set_properties(self, name, color=None, linetype=None, lineweight=None): return await self._dispatch("layer-set-properties", locals_without_self(locals()))
    async def layer_freeze(self, name): return await self._dispatch("layer-freeze", {"name": name})
    async def layer_thaw(self, name): return await self._dispatch("layer-thaw", {"name": name})
    async def layer_lock(self, name): return await self._dispatch("layer-lock", {"name": name})
    async def layer_unlock(self, name): return await self._dispatch("layer-unlock", {"name": name})

    # Block operations
    async def block_list(self): return await self._dispatch("block-list", {})
    async def block_insert(self, name, x, y, scale=1.0, rotation=0.0, block_id=None): return await self._dispatch("block-insert", locals_without_self(locals()))
    async def block_insert_with_attributes(self, name, x, y, scale=1.0, rotation=0.0, attributes=None):
        return await self._dispatch("block-insert-with-attributes", {"name": name, "x": x, "y": y, "scale": scale, "rotation": rotation, "attributes_str": encode_attributes(attributes)})
    async def block_get_attributes(self, entity_id): return await self._dispatch("block-get-attributes", {"entity_id": entity_id})
    async def block_update_attribute(self, entity_id, tag, value): return await self._dispatch("block-update-attribute", locals_without_self(locals()))
    async def block_define(self, name, entities): return await self._dispatch("block-define", {"name": name, "entities": entities})

    # Annotation
    async def create_text(self, x, y, text, height=2.5, rotation=0.0, layer=None): return await self._dispatch("create-text", locals_without_self(locals()))
    async def create_dimension_linear(self, x1, y1, x2, y2, dim_x, dim_y): return await self._dispatch("create-dimension-linear", locals_without_self(locals()))
    async def create_dimension_aligned(self, x1, y1, x2, y2, offset): return await self._dispatch("create-dimension-aligned", locals_without_self(locals()))
    async def create_dimension_angular(self, cx, cy, x1, y1, x2, y2): return await self._dispatch("create-dimension-angular", locals_without_self(locals()))
    async def create_dimension_radius(self, cx, cy, radius, angle): return await self._dispatch("create-dimension-radius", locals_without_self(locals()))
    async def create_leader(self, points, text): return await self._dispatch("create-leader", {"points_str": ";".join(f"{p[0]},{p[1]}" for p in points), "text": text})

    # P&ID
    async def pid_setup_layers(self): return await self._dispatch("pid-setup-layers", {})
    async def pid_insert_symbol(self, category, symbol, x, y, scale=1.0, rotation=0.0): return await self._dispatch("pid-insert-symbol", locals_without_self(locals()))
    async def pid_list_symbols(self, category): return await self._dispatch("pid-list-symbols", {"category": category})
    async def pid_draw_process_line(self, x1, y1, x2, y2): return await self._dispatch("pid-draw-process-line", locals_without_self(locals()))
    async def pid_connect_equipment(self, x1, y1, x2, y2): return await self._dispatch("pid-connect-equipment", locals_without_self(locals()))
    async def pid_add_flow_arrow(self, x, y, rotation=0.0): return await self._dispatch("pid-add-flow-arrow", locals_without_self(locals()))
    async def pid_add_equipment_tag(self, x, y, tag, description=""): return await self._dispatch("pid-add-equipment-tag", locals_without_self(locals()))
    async def pid_add_line_number(self, x, y, line_num, spec): return await self._dispatch("pid-add-line-number", locals_without_self(locals()))
    async def pid_insert_valve(self, x, y, valve_type, rotation=0.0, attributes=None):
        return await self._dispatch("pid-insert-valve", {"x": x, "y": y, "valve_type": valve_type, "rotation": rotation, "attributes_str": encode_attributes(attributes)})
    async def pid_insert_instrument(self, x, y, instrument_type, rotation=0.0, tag_id="", range_value=""): return await self._dispatch("pid-insert-instrument", locals_without_self(locals()))
    async def pid_insert_pump(self, x, y, pump_type, rotation=0.0, attributes=None):
        return await self._dispatch("pid-insert-pump", {"x": x, "y": y, "pump_type": pump_type, "rotation": rotation, "attributes_str": encode_attributes(attributes)})
    async def pid_insert_tank(self, x, y, tank_type, scale=1.0, attributes=None):
        return await self._dispatch("pid-insert-tank", {"x": x, "y": y, "tank_type": tank_type, "scale": scale, "attributes_str": encode_attributes(attributes)})

    # View
    async def zoom_extents(self): return await self._dispatch("zoom-extents", {})
    async def zoom_window(self, x1, y1, x2, y2): return await self._dispatch("zoom-window", locals_without_self(locals()))
    async def get_screenshot(self):
        if self._screenshot_provider:
            data = self._screenshot_provider.capture()
            if data:
                return CommandResult(True, payload=data)
        return _error("screenshot_failed", "Screenshot capture failed.")


def locals_without_self(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key != "self"}
