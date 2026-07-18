"""Lazy backend singleton, _safe/_error/_json helpers, screenshot utility."""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import time
import uuid
from typing import Any

import structlog
from mcp.types import ImageContent, TextContent

from autocad_mcp.backends.base import AutoCADBackend, CommandResult
from autocad_mcp.config import ONLY_TEXT_FEEDBACK, TransportConfig, detect_backend, load_transport_config
from autocad_mcp.remote_policy import evaluate_operation

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Lazy backend singleton
# ---------------------------------------------------------------------------

_backend: AutoCADBackend | None = None
_init_lock = asyncio.Lock()


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

        if backend_name == "file_ipc":
            from autocad_mcp.backends.file_ipc import FileIPCBackend

            backend = FileIPCBackend()
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
    """Format an exception with an actionable hint."""
    msg = str(e)
    msg_lower = msg.lower()

    if "window not found" in msg_lower or "no autocad" in msg_lower:
        hint = "AutoCAD LT is not running or no drawing is open. Start AutoCAD and open a .dwg file."
    elif "timeout" in msg_lower:
        hint = "Command timed out. AutoCAD may be in a modal dialog. Press ESC in AutoCAD and retry."
    elif "not supported" in msg_lower or "backend" in msg_lower:
        hint = "Operation not supported on current backend. Check system(operation='status') for capabilities."
    elif "dispatcher" in msg_lower or "mcp_dispatch" in msg_lower:
        hint = "mcp_dispatch.lsp not loaded. In AutoCAD command line, type: (load \"mcp_dispatch.lsp\")"
    else:
        hint = "Unexpected error. Check AutoCAD is responsive and retry."

    return _json({"error": f"[{context}] {msg}" if context else msg, "hint": hint})


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
            operation = str(kwargs.get("operation", "unknown"))
            config: TransportConfig | None = None
            try:
                config = load_transport_config()
                decision = evaluate_operation(
                    tool=tool_name,
                    operation=operation,
                    data=kwargs.get("data"),
                    config=config,
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
                            "request_id": request_id,
                        }
                    )

                result = await fn(*args, **kwargs)
                _audit(
                    request_id=request_id,
                    config=config,
                    tool=tool_name,
                    operation=operation,
                    decision="allow",
                    outcome=_result_outcome(result),
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
                op = kwargs.get("operation", "unknown")
                log.error("tool_error", tool=tool_name, operation=op, error=str(e))
                return _error(e, f"{tool_name}.{op}")

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
        config = load_transport_config()
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
