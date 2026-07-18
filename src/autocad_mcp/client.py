"""Lazy backend singleton, _safe/_error/_json helpers, screenshot utility."""

from __future__ import annotations

import asyncio
import base64
import functools
import inspect
import json
import time
import uuid
from typing import Any

import structlog
from mcp.types import ImageContent, TextContent
from mcp.server.auth.middleware.auth_context import get_access_token

from autocad_mcp.backends.base import AutoCADBackend, CommandResult
from autocad_mcp.config import (
    ONLY_TEXT_FEEDBACK,
    TransportConfig,
    detect_backend,
    get_active_transport_config,
    load_transport_config,
)
from autocad_mcp.remote_policy import evaluate_operation

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Lazy backend singleton
# ---------------------------------------------------------------------------

_backend: AutoCADBackend | None = None
_init_lock = asyncio.Lock()


def _current_transport_config() -> TransportConfig:
    """Use the current HTTP app config, falling back to process environment."""

    return get_active_transport_config() or load_transport_config()


async def get_backend() -> AutoCADBackend:
    """Return (and lazily initialize) the backend singleton.

    Uses an asyncio Lock to prevent concurrent initialization races
    when multiple MCP tool calls arrive simultaneously.
    """
    global _backend
    if _backend is not None:
        return _backend

    async with _init_lock:
        # Double-check after acquiring lock (another task may have initialized)
        if _backend is not None:
            return _backend

        backend_name = detect_backend()
        transport_config = _current_transport_config()

        if backend_name == "file_ipc":
            from autocad_mcp.backends.safe_file_ipc import SafeFileIPCBackend

            backend = SafeFileIPCBackend(
                allow_execute_lisp=not (
                    transport_config.transport == "streamable-http"
                    and transport_config.remote_profile != "off"
                )
            )
        else:
            from autocad_mcp.backends.ezdxf_backend import EzdxfBackend

            backend = EzdxfBackend()

        result = await backend.initialize()
        if not result.ok:
            raise RuntimeError(f"Backend init failed: {result.error}")

        _backend = backend
        log.info("backend_initialized", backend=_backend.name)
        return _backend


# ---------------------------------------------------------------------------
# JSON serialization helper
# ---------------------------------------------------------------------------


def _json(data: Any) -> str:
    """Serialize to compact JSON string."""
    return json.dumps(data, default=str, separators=(",", ":"))


def _result_outcome(result: Any) -> str:
    """Classify a tool return value without logging its content."""

    texts: list[str] = []
    if isinstance(result, str):
        texts.append(result)
    elif isinstance(result, list):
        texts.extend(
            item.text
            for item in result
            if isinstance(getattr(item, "text", None), str)
        )

    for text in texts:
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("ok") is False:
            return "error"
    return "ok"


def _bound_tool_arguments(fn, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, Any]:
    """Read policy inputs from positional or keyword tool arguments."""

    bound = inspect.signature(fn).bind_partial(*args, **kwargs)
    return str(bound.arguments.get("operation", "unknown")), bound.arguments.get("data")


def _audit(
    *,
    request_id: str,
    config: TransportConfig,
    tool: str,
    operation: str,
    decision: str,
    outcome: str,
    started_at: float,
) -> None:
    """Write a safe audit record; never include tokens, paths, or payloads."""

    log.info(
        "mcp_audit",
        request_id=request_id,
        profile=config.remote_profile,
        auth_mode=config.auth_mode,
        transport=config.transport,
        tool=tool,
        operation=operation,
        decision=decision,
        outcome=outcome,
        duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
        backend=getattr(_backend, "name", None),
    )


# ---------------------------------------------------------------------------
# Error formatting with actionable hints
# ---------------------------------------------------------------------------


def _error(e: Exception, context: str = "") -> str:
    """Format exceptions without guessing that every timeout means LISP is absent."""
    msg = str(e)
    msg_lower = msg.lower()

    if "window not found" in msg_lower or "no autocad" in msg_lower:
        code = "autocad_not_running"
        hint = "Start AutoCAD LT and open a drawing."
    elif "dispatcher_missing_in_active_document" in msg_lower:
        code = "dispatcher_missing_in_active_document"
        hint = "Configure acadltdoc.lsp to load mcp_dispatch.lsp for every document; do not use APPLOAD as recovery."
    elif "no active document" in msg_lower or "active document" in msg_lower:
        code = "no_active_document"
        hint = "Open or activate a drawing in AutoCAD LT."
    elif "modal" in msg_lower or "dialog" in msg_lower:
        code = "modal_dialog_active"
        hint = "Close the existing AutoCAD dialog, then retry. MCP will not open APPLOAD or change focus."
    elif "busy" in msg_lower or "command active" in msg_lower:
        code = "autocad_busy"
        hint = "Finish or cancel the command you started in AutoCAD, then retry. MCP does not send ESC."
    elif "timeout" in msg_lower:
        code = "dispatcher_timeout"
        hint = "The dispatcher did not answer in time. Check AutoCAD busy/dialog state and system.health; timeout alone does not prove LISP is missing."
    elif "routing" in msg_lower or "com" in msg_lower or "activex" in msg_lower:
        code = "command_routing_failed"
        hint = "Run the MCP server with native Windows Python and verify AutoCAD ActiveX/COM is available."
    elif "not supported" in msg_lower or "backend" in msg_lower:
        code = "unsupported_operation"
        hint = "Operation not supported on the current backend. Check system(operation='status')."
    else:
        code = "unexpected_error"
        hint = "Check system(operation='health') for the precise AutoCAD/dispatcher state."

    return _json(
        {
            "ok": False,
            "error_code": code,
            "error": f"[{context}] {msg}" if context else msg,
            "hint": hint,
        }
    )


# ---------------------------------------------------------------------------
# _safe decorator for tool error handling
# ---------------------------------------------------------------------------


def _safe(tool_name: str):
    """Wrap an async tool handler with uniform error handling."""

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            request_id = uuid.uuid4().hex[:12]
            started_at = time.perf_counter()
            operation, data = _bound_tool_arguments(fn, args, kwargs)
            config: TransportConfig | None = None
            try:
                config = _current_transport_config()
                access_token = get_access_token()
                decision = evaluate_operation(
                    tool=tool_name,
                    operation=operation,
                    data=data,
                    config=config,
                    scopes=(access_token.scopes if access_token else None),
                )
                if not decision.allowed:
                    _audit(
                        request_id=request_id,
                        config=config,
                        tool=tool_name,
                        operation=operation,
                        decision=f"deny:{decision.code}",
                        outcome="denied",
                        started_at=started_at,
                    )
                    return _json(
                        {
                            "ok": False,
                            "error": f"Remote policy denied {tool_name}.{operation}: "
                            f"{decision.reason}",
                            "code": decision.code,
                            "request_id": request_id,
                        }
                    )

                result = await fn(*args, **kwargs)
                outcome = _result_outcome(result)
                _audit(
                    request_id=request_id,
                    config=config,
                    tool=tool_name,
                    operation=operation,
                    decision="allow",
                    outcome=outcome,
                    started_at=started_at,
                )
                return result
            except Exception as e:
                if config is not None:
                    _audit(
                        request_id=request_id,
                        config=config,
                        tool=tool_name,
                        operation=operation,
                        decision="allow",
                        outcome="error",
                        started_at=started_at,
                    )
                log.error("tool_error", tool=tool_name, operation=operation, error=str(e))
                return _error(e, f"{tool_name}.{operation}")

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Screenshot helper
# ---------------------------------------------------------------------------


def _format_result(
    result: CommandResult,
    include_screenshot: bool = False,
    screenshot_data: str | None = None,
) -> list[TextContent | ImageContent] | str:
    """Format a CommandResult for MCP response.

    Returns a list with TextContent + optional ImageContent if screenshot requested,
    or a plain JSON string if no screenshot.
    """
    text = _json(result.to_dict())

    if not include_screenshot or ONLY_TEXT_FEEDBACK or not screenshot_data:
        return text

    return [
        TextContent(type="text", text=text),
        ImageContent(
            type="image",
            data=screenshot_data,
            mimeType="image/png",
        ),
    ]


async def add_screenshot_if_available(
    result: CommandResult,
    include_screenshot: bool = False,
) -> list[TextContent | ImageContent] | str:
    """Conditionally append a screenshot to the result."""
    if not include_screenshot or ONLY_TEXT_FEEDBACK:
        return _json(result.to_dict())

    backend = await get_backend()
    screenshot_result = await backend.get_screenshot()

    if screenshot_result.ok and screenshot_result.payload:
        config = _current_transport_config()
        if config.transport == "streamable-http" and config.remote_profile != "off":
            try:
                image_bytes = len(base64.b64decode(screenshot_result.payload, validate=True))
            except (ValueError, TypeError):
                log.warning("screenshot_rejected", reason="invalid_base64")
                return _json({"ok": False, "error": "Screenshot payload is invalid."})
            if image_bytes > config.max_image_bytes:
                log.warning(
                    "screenshot_rejected",
                    image_bytes=image_bytes,
                    max_image_bytes=config.max_image_bytes,
                )
                return _json(
                    {
                        "ok": False,
                        "error": "Screenshot exceeds the configured remote image size limit.",
                        "max_image_bytes": config.max_image_bytes,
                    }
                )
        return _format_result(result, True, screenshot_result.payload)

    return _json(result.to_dict())


def format_screenshot_response(
    screenshot_result: CommandResult,
) -> list[TextContent | ImageContent] | str:
    """Format a direct screenshot while enforcing remote payload limits."""

    if not screenshot_result.ok or not screenshot_result.payload:
        return _json(screenshot_result.to_dict())

    config = _current_transport_config()
    if config.transport == "streamable-http" and config.remote_profile != "off":
        try:
            image_bytes = len(base64.b64decode(screenshot_result.payload, validate=True))
        except (ValueError, TypeError):
            log.warning("screenshot_rejected", reason="invalid_base64")
            return _json({"ok": False, "error": "Screenshot payload is invalid."})
        if image_bytes > config.max_image_bytes:
            log.warning(
                "screenshot_rejected",
                image_bytes=image_bytes,
                max_image_bytes=config.max_image_bytes,
            )
            return _json(
                {
                    "ok": False,
                    "error": "Screenshot exceeds the configured remote image size limit.",
                    "max_image_bytes": config.max_image_bytes,
                }
            )

    return [
        TextContent(type="text", text=_json({"ok": True, "screenshot": "attached"})),
        ImageContent(type="image", data=screenshot_result.payload, mimeType="image/png"),
    ]
