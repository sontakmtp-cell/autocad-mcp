"""Phase 2 ActiveX dimension commit integration.

The AutoLISP override creates dimensions directly with ModelSpace.AddDim*.
This Python hook preserves the existing plan format while passing the planned
center-mark size to the commit file.
"""

from __future__ import annotations

from autocad_mcp import dimension_workflow
from autocad_mcp.dimension_plans import PlannedDimension

_INSTALLED = False
_ORIGINAL_DIMENSION_TO_LISP_DATA = dimension_workflow._dimension_to_lisp_data


def _phase2_dimension_to_lisp_data(item: PlannedDimension) -> str:
    if item.kind != "center":
        return _ORIGINAL_DIMENSION_TO_LISP_DATA(item)

    entity_id = str(item.geometry["entity_id"])
    size = float(item.geometry.get("size", 0.0) or 0.0)
    return (
        f'("center" {dimension_workflow._lisp_string(entity_id)} '
        f"{size:.12g})"
    )


def install() -> None:
    """Install the richer center-mark serializer once per MCP process."""

    global _INSTALLED
    if _INSTALLED:
        return
    dimension_workflow._dimension_to_lisp_data = _phase2_dimension_to_lisp_data
    _INSTALLED = True
