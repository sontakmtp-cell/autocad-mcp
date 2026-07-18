"""Fail-closed policy wrapper around the reliable File IPC backend."""

from __future__ import annotations

from autocad_mcp.backends.base import CommandResult
from autocad_mcp.backends.file_ipc import FileIPCBackend

SUPPORTED_IPC_COMMANDS = frozenset(
    {
        "ping",
        "drawing-info",
        "drawing-save",
        "drawing-save-as-dxf",
        "drawing-create",
        "drawing-purge",
        "drawing-plot-pdf",
        "drawing-get-variables",
        "drawing-open",
        "undo",
        "redo",
        "execute-lisp",
        "create-line",
        "create-circle",
        "create-polyline",
        "create-rectangle",
        "create-arc",
        "create-ellipse",
        "create-mtext",
        "create-hatch",
        "entity-list",
        "entity-count",
        "entity-get",
        "entity-erase",
        "entity-copy",
        "entity-move",
        "entity-rotate",
        "entity-scale",
        "entity-mirror",
        "entity-offset",
        "entity-array",
        "entity-fillet",
        "entity-chamfer",
        "layer-list",
        "layer-create",
        "layer-set-current",
        "layer-set-properties",
        "layer-freeze",
        "layer-thaw",
        "layer-lock",
        "layer-unlock",
        "block-list",
        "block-insert",
        "block-insert-with-attributes",
        "block-get-attributes",
        "block-update-attribute",
        "block-define",
        "create-text",
        "create-dimension-linear",
        "create-dimension-aligned",
        "create-dimension-angular",
        "create-dimension-radius",
        "create-leader",
        "pid-setup-layers",
        "pid-insert-symbol",
        "pid-list-symbols",
        "pid-draw-process-line",
        "pid-connect-equipment",
        "pid-add-flow-arrow",
        "pid-add-equipment-tag",
        "pid-add-line-number",
        "pid-insert-valve",
        "pid-insert-instrument",
        "pid-insert-pump",
        "pid-insert-tank",
        "zoom-extents",
        "zoom-window",
    }
)


class SafeFileIPCBackend(FileIPCBackend):
    """Validate File IPC commands without touching a user's active command."""

    def __init__(self, *, allow_execute_lisp: bool = True):
        super().__init__()
        self._allow_execute_lisp = allow_execute_lisp

    async def execute_lisp(self, code: str) -> CommandResult:
        """Reject remote LISP before creating a temp file or IPC request."""

        if not self._allow_execute_lisp:
            return CommandResult(
                ok=False,
                error="execute_lisp is disabled for remote File IPC profiles",
                error_code="execute_lisp_denied",
            )
        return await super().execute_lisp(code)

    async def _dispatch(
        self,
        command: str,
        params: dict,
        retry_ping: bool = False,
    ) -> CommandResult:
        """Validate before IPC; never send ESC or simulate command-line input."""

        if command not in SUPPORTED_IPC_COMMANDS:
            return CommandResult(
                ok=False,
                error=f"Unsupported File IPC command: {command}",
                error_code="unsupported_operation",
            )
        if command == "execute-lisp" and not self._allow_execute_lisp:
            return CommandResult(
                ok=False,
                error="execute_lisp is disabled for remote File IPC profiles",
                error_code="execute_lisp_denied",
            )

        try:
            return await super()._dispatch(command, params, retry_ping=retry_ping)
        except Exception as exc:
            return CommandResult(
                ok=False,
                error=f"File IPC dispatch failed: {exc}",
                error_code="command_routing_failed",
            )
