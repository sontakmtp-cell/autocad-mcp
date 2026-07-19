"""MCP registration for one-call automatic dimensioning."""

from __future__ import annotations

from autocad_mcp.autodim import (
    AutoDimensionOptions,
    run_ezdxf_auto_dimension,
    run_file_ipc_auto_dimension,
)
from autocad_mcp.client import _safe, add_screenshot_if_available, get_backend
from autocad_mcp.server import ToolResult, mcp


@_safe("annotation")
async def _run_auto_dimension(
    *,
    operation: str,
    data: dict | None,
    include_screenshot: bool,
) -> ToolResult:
    options = AutoDimensionOptions.from_data(data)
    backend = await get_backend()

    if backend.name == "file_ipc":
        result = await run_file_ipc_auto_dimension(backend, options)
    elif backend.name == "ezdxf":
        result = await run_ezdxf_auto_dimension(backend, options)
    else:
        raise RuntimeError(f"Automatic dimensioning is not supported by backend {backend.name!r}")

    return await add_screenshot_if_available(result, include_screenshot)


@mcp.tool(
    name="annotation.auto_dimension",
    annotations={
        "title": "Auto-dimension AutoCAD Model Space",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def annotation_auto_dimension(
    data: dict | None = None,
    include_screenshot: bool = True,
) -> ToolResult:
    """Analyze Model Space and create a clean dimension layout in one call.

    The AutoCAD plugin reads LINE/POLYLINE/CIRCLE/ARC geometry locally, detects
    overall extents, holes, arcs and symmetric hole pairs, then places dimensions
    in deterministic lanes outside the part to reduce overlap. A preview image is
    attached by default.

    data options:
      mode: "minimal" | "balanced" | "detailed" (default "balanced")
      include_overall: bool (default true)
      include_features: bool (default true)
      include_holes: bool (default true; circles use diameter dimensions)
      include_arcs: bool (default true; arcs use radius dimensions)
      include_centers: bool (default true)
      detect_symmetry: bool (default true)
      clear_existing: bool (default false; only clears dimensions on dimension_layer)
      zoom_preview: bool (default true)
      dimension_layer: str (default "MCP-DIM")
      spacing: positive number or omit for automatic spacing
      source_layers: optional list of geometry layers to analyze

    All generated dimensions are grouped into one AutoCAD UNDO step.
    """

    return await _run_auto_dimension(
        operation="auto_dimension",
        data=data,
        include_screenshot=include_screenshot,
    )
