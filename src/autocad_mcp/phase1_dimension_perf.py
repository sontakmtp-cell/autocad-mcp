"""Phase 1 performance improvements for AutoCAD dimension workflows.

This module is intentionally additive. It patches the existing preview-first
workflow at process startup so the public API remains backward compatible while
fast paths avoid redundant geometry exports and repeated heavy LISP loads.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Mapping

import structlog

from autocad_mcp import auto_dimension_tool as annotation_tools
from autocad_mcp.backends.base import CommandResult
from autocad_mcp.client import _safe, add_screenshot_if_available, get_backend
from autocad_mcp.config import LISP_DIR
from autocad_mcp.dimension_workflow import (
    build_dimension_candidates,
    collect_dimension_records,
    commit_dimension_plan,
    drawing_fingerprint,
    geometry_only,
    records_fingerprint,
)
from autocad_mcp.part_detection import GeometrySelection, detect_parts, select_records
from autocad_mcp.server import ToolResult, mcp

log = structlog.get_logger()

_INSTALLED = False
_ORIGINAL_RUN_ANNOTATION = annotation_tools._run_annotation
_LOADER_PATH = (LISP_DIR / "auto_dimension_loader.lsp").resolve()


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _point(value: Any, field_name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ValueError(f"{field_name} must be a two-number point")
    try:
        return [float(value[0]), float(value[1])]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a two-number point") from exc


def _flat_point(
    item: Mapping[str, Any],
    x_name: str,
    y_name: str,
    field_name: str,
) -> list[float]:
    if x_name not in item or y_name not in item:
        raise ValueError(f"{field_name} is required")
    try:
        return [float(item[x_name]), float(item[y_name])]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain numeric coordinates") from exc


def _normalize_batch_dimension(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ValueError(f"dimensions[{index}] must be an object")

    raw_kind = str(item.get("kind", item.get("type", ""))).strip().lower()
    aliases = {
        "dimension_linear": "linear",
        "create_dimension_linear": "linear",
        "dimension_diameter": "diameter",
        "dimension_radius": "radius",
        "center_mark": "center",
        "create_text": "text",
    }
    kind = aliases.get(raw_kind, raw_kind)
    if kind not in {"linear", "diameter", "radius", "center", "text"}:
        raise ValueError(
            f"dimensions[{index}].kind must be linear, diameter, radius, center, or text"
        )

    geometry_raw = item.get("geometry", {})
    placement_raw = item.get("placement", {})
    metadata_raw = item.get("metadata", {})
    if not isinstance(geometry_raw, Mapping):
        raise ValueError(f"dimensions[{index}].geometry must be an object")
    if not isinstance(placement_raw, Mapping):
        raise ValueError(f"dimensions[{index}].placement must be an object")
    if not isinstance(metadata_raw, Mapping):
        raise ValueError(f"dimensions[{index}].metadata must be an object")

    geometry = copy.deepcopy(dict(geometry_raw))
    placement = copy.deepcopy(dict(placement_raw))
    candidate: dict[str, Any] = {
        "kind": kind,
        "geometry": geometry,
        "placement": placement,
        "metadata": copy.deepcopy(dict(metadata_raw)),
    }
    if item.get("text") is not None:
        candidate["text"] = str(item["text"])

    if kind == "linear":
        p1_value = geometry.get("p1", item.get("p1"))
        if p1_value is None:
            p1_value = _flat_point(item, "x1", "y1", f"dimensions[{index}].p1")
        p2_value = geometry.get("p2", item.get("p2"))
        if p2_value is None:
            p2_value = _flat_point(item, "x2", "y2", f"dimensions[{index}].p2")
        base_value = placement.get("base", item.get("base"))
        if base_value is None:
            base_value = _flat_point(item, "dim_x", "dim_y", f"dimensions[{index}].base")

        geometry["p1"] = _point(p1_value, f"dimensions[{index}].p1")
        geometry["p2"] = _point(p2_value, f"dimensions[{index}].p2")
        placement["base"] = _point(base_value, f"dimensions[{index}].base")
        try:
            placement["angle"] = float(placement.get("angle", item.get("angle", 0.0)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"dimensions[{index}].angle must be numeric") from exc
        placement.setdefault("label_anchor", list(placement["base"]))

    elif kind in {"diameter", "radius", "center"}:
        entity_id = geometry.get("entity_id", item.get("entity_id"))
        if entity_id in (None, ""):
            raise ValueError(f"dimensions[{index}].entity_id is required for {kind}")
        geometry["entity_id"] = str(entity_id)
        if kind in {"diameter", "radius"}:
            point_value = geometry.get("point", item.get("point"))
            if point_value is None:
                point_value = _flat_point(item, "x", "y", f"dimensions[{index}].point")
            geometry["point"] = _point(point_value, f"dimensions[{index}].point")
            placement.setdefault("label_anchor", list(geometry["point"]))

    elif kind == "text":
        point_value = geometry.get("point", item.get("point"))
        if point_value is None:
            point_value = _flat_point(item, "x", "y", f"dimensions[{index}].point")
        geometry["point"] = _point(point_value, f"dimensions[{index}].point")
        placement.setdefault("label_anchor", list(geometry["point"]))
        if not candidate.get("text"):
            raise ValueError(f"dimensions[{index}].text is required for text")

    return candidate


def normalize_batch_dimensions(value: Any) -> list[dict[str, Any]]:
    """Normalize compact or plan-shaped batch items into plan candidates."""

    if not isinstance(value, list) or not value:
        raise ValueError("data.dimensions must be a non-empty array")
    return [_normalize_batch_dimension(item, index) for index, item in enumerate(value)]


async def _timed_new_plan(data: dict[str, Any]) -> tuple[Any, list[Any], dict[str, Any]]:
    timings: dict[str, float] = {}
    total_started = time.perf_counter()

    started = time.perf_counter()
    options = annotation_tools.AutoDimensionOptions.from_data(data)
    profile = annotation_tools._resolve_profile(data)
    selection = GeometrySelection.from_data(data)
    if options.clear_existing and selection.is_active:
        raise ValueError(
            "clear_existing cannot be combined with a part/region/entity selector; "
            "use audit_dimensions and repair_dimension_layout to avoid deleting another part"
        )
    timings["parse_options"] = _elapsed_ms(started)

    started = time.perf_counter()
    backend = await get_backend()
    timings["get_backend"] = _elapsed_ms(started)

    started = time.perf_counter()
    records = geometry_only(
        await collect_dimension_records(
            backend,
            dimension_layer=profile.layer,
            source_layers=options.source_layers,
            use_current_selection=selection.use_current_selection,
        )
    )
    export_metrics = copy.deepcopy(
        getattr(backend, "_last_dimension_export_metrics", {}) or {}
    )
    timings["export_geometry"] = _elapsed_ms(started)

    started = time.perf_counter()
    gap_value = data.get("part_gap_tolerance")
    parts = detect_parts(
        records,
        gap_tolerance=None if gap_value in (None, "") else float(gap_value),
    )
    timings["detect_parts"] = _elapsed_ms(started)

    started = time.perf_counter()
    selected = select_records(records, selection, parts=parts)
    if not selected:
        raise ValueError("The requested target contains no supported geometry")
    timings["select_target"] = _elapsed_ms(started)

    started = time.perf_counter()
    candidates, analysis = build_dimension_candidates(
        selected,
        options=options,
        profile=profile,
    )
    timings["build_candidates"] = _elapsed_ms(started)

    started = time.perf_counter()
    document_id = drawing_fingerprint(backend)
    selected_fingerprint = records_fingerprint(selected)
    timings["fingerprint"] = _elapsed_ms(started)
    timings["plan_before_store"] = _elapsed_ms(total_started)

    analysis = {
        **analysis,
        "timings_ms": dict(timings),
        "export_metrics": export_metrics,
    }
    target = {
        "target_part_id": selection.target_part_id,
        "region": selection.region.to_dict() if selection.region else None,
        "entity_ids": list(selection.entity_ids),
        "use_current_selection": selection.use_current_selection,
        "selected_entity_ids": [record.handle for record in selected],
        "clear_existing": options.clear_existing,
        "analysis": analysis,
    }

    started = time.perf_counter()
    plan = annotation_tools._plans.create(
        candidates,
        profile_name=profile.name,
        target=target,
    )
    timings["store_plan"] = _elapsed_ms(started)
    timings["plan_total"] = _elapsed_ms(total_started)

    active_plan_ids = annotation_tools._plans.plan_ids()
    for stale_plan_id in set(annotation_tools._plan_context) - active_plan_ids:
        annotation_tools._plan_context.pop(stale_plan_id, None)
    annotation_tools._plan_context[plan.plan_id] = {
        "records": selected,
        "selection": selection,
        "profile": profile,
        "source_layers": options.source_layers,
        "drawing_fingerprint": document_id,
        "records_fingerprint": selected_fingerprint,
        "phase1_timings_ms": timings,
        "export_metrics": export_metrics,
    }
    log.info("dimension_plan_timing", operation="new_plan", **timings)
    return plan, selected, analysis


async def _run_fast_auto_dimension(raw: dict[str, Any], include_image: bool) -> ToolResult:
    total_started = time.perf_counter()
    plan, _records, _analysis = await annotation_tools._new_plan(raw)
    context = annotation_tools._plan_context[plan.plan_id]
    timings = dict(context.get("phase1_timings_ms", {}))

    started = time.perf_counter()
    backend = await get_backend()
    if drawing_fingerprint(backend) != context["drawing_fingerprint"]:
        raise ValueError(
            "The active drawing changed while automatic dimensioning was being prepared; retry"
        )
    timings["fast_document_check"] = _elapsed_ms(started)

    started = time.perf_counter()

    async def executor(plan_to_commit):
        return await commit_dimension_plan(backend, plan_to_commit, context["profile"])

    committed = await annotation_tools._plans.commit(
        plan.plan_id,
        executor,
        expected_revision=plan.revision,
    )
    timings["commit"] = _elapsed_ms(started)
    timings["server_before_screenshot"] = _elapsed_ms(total_started)

    result = CommandResult(
        ok=True,
        payload={
            "plan_id": committed.plan_id,
            "profile_name": committed.profile_name,
            "target": committed.target,
            "dimensions": [item.to_dict() for item in committed.dimensions],
            "timings_ms": timings,
            "fast_path": "single_export",
            **(committed.commit_result or {}),
            **annotation_tools._normalized_dimension_commit_result(
                committed=committed,
                backend=backend,
                context=context,
                timings=timings,
            ),
        },
    )

    started = time.perf_counter()
    response = await add_screenshot_if_available(result, include_image)
    timings["screenshot"] = _elapsed_ms(started)
    timings["total"] = _elapsed_ms(total_started)
    log.info("dimension_phase1_timing", operation="auto_dimension", **timings)
    return response


async def _run_batch_create(raw: dict[str, Any], include_image: bool) -> ToolResult:
    total_started = time.perf_counter()
    timings: dict[str, float] = {}

    started = time.perf_counter()
    candidates = normalize_batch_dimensions(raw.get("dimensions"))
    profile = annotation_tools._resolve_profile(raw)
    clear_existing = annotation_tools._boolean(raw.get("clear_existing"), default=False)
    timings["normalize_batch"] = _elapsed_ms(started)

    started = time.perf_counter()
    backend = await get_backend()
    document_id = drawing_fingerprint(backend)
    timings["get_backend_and_document"] = _elapsed_ms(started)

    started = time.perf_counter()
    plan = annotation_tools._plans.create(
        candidates,
        profile_name=profile.name,
        target={
            "manual_batch": True,
            "clear_existing": clear_existing,
            "drawing_fingerprint": document_id,
        },
    )
    timings["store_plan"] = _elapsed_ms(started)

    started = time.perf_counter()

    async def executor(plan_to_commit):
        return await commit_dimension_plan(backend, plan_to_commit, profile)

    committed = await annotation_tools._plans.commit(
        plan.plan_id,
        executor,
        expected_revision=plan.revision,
    )
    timings["commit"] = _elapsed_ms(started)
    timings["server_before_screenshot"] = _elapsed_ms(total_started)

    result = CommandResult(
        ok=True,
        payload={
            "plan_id": committed.plan_id,
            "profile_name": committed.profile_name,
            "dimension_count": len(committed.dimensions),
            "dimensions": [item.to_dict() for item in committed.dimensions],
            "timings_ms": timings,
            "fast_path": "manual_batch",
            **(committed.commit_result or {}),
            **annotation_tools._normalized_dimension_commit_result(
                committed=committed,
                backend=backend,
                timings=timings,
                manual_batch=True,
            ),
        },
    )

    started = time.perf_counter()
    response = await add_screenshot_if_available(result, include_image)
    timings["screenshot"] = _elapsed_ms(started)
    timings["total"] = _elapsed_ms(total_started)
    log.info("dimension_phase1_timing", operation="batch_create_dimensions", **timings)
    return response


@_safe("annotation")
async def _run_phase1_operation(
    *,
    operation: str,
    data: dict | None,
    include_image: bool,
) -> ToolResult:
    raw = data or {}
    if operation == "auto_dimension":
        return await _run_fast_auto_dimension(raw, include_image)
    if operation == "batch_create_dimensions":
        return await _run_batch_create(raw, include_image)
    raise ValueError(f"Unsupported phase 1 annotation operation: {operation}")


async def _patched_run_annotation(
    *,
    operation: str,
    data: dict | None,
    include_image: bool,
) -> ToolResult:
    if operation in {"auto_dimension", "batch_create_dimensions"}:
        return await _run_phase1_operation(
            operation=operation,
            data=data,
            include_image=include_image,
        )
    return await _ORIGINAL_RUN_ANNOTATION(
        operation=operation,
        data=data,
        include_image=include_image,
    )


def _patch_file_ipc_lisp_loader() -> None:
    """Replace the heavy engine path with a tiny version-aware loader."""

    from autocad_mcp.backends.file_ipc import FileIPCBackend

    if getattr(FileIPCBackend, "_phase1_dimension_loader_installed", False):
        return
    if not _LOADER_PATH.exists():
        raise RuntimeError(f"Dimension LISP loader is missing: {_LOADER_PATH}")

    method_names = (
        "annotation_export_dimension_geometry",
        "annotation_commit_dimension_plan",
        "annotation_repair_dimensions",
    )
    for method_name in method_names:
        original = getattr(FileIPCBackend, method_name)

        async def wrapped(self, _original=original, **kwargs):
            kwargs["lisp_path"] = str(_LOADER_PATH)
            return await _original(self, **kwargs)

        wrapped.__name__ = original.__name__
        wrapped.__doc__ = original.__doc__
        setattr(FileIPCBackend, method_name, wrapped)

    FileIPCBackend._phase1_dimension_loader_installed = True


def _append_tool_guidance(tool_name: str, guidance: str) -> None:
    """Best-effort update for FastMCP 1.x tool descriptions already registered."""

    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict):
        return
    tool = tools.get(tool_name)
    if tool is None:
        return
    current = str(getattr(tool, "description", "") or "")
    if guidance in current:
        return
    updated = (current.rstrip() + "\n\n" + guidance).strip()
    try:
        setattr(tool, "description", updated)
    except (AttributeError, TypeError):
        try:
            object.__setattr__(tool, "description", updated)
        except (AttributeError, TypeError):
            log.warning("dimension_tool_guidance_not_updated", tool=tool_name)


def install() -> None:
    """Install the phase 1 fast paths exactly once per MCP process."""

    global _INSTALLED
    if _INSTALLED:
        return
    _patch_file_ipc_lisp_loader()
    annotation_tools._new_plan = _timed_new_plan
    annotation_tools._run_annotation = _patched_run_annotation
    guidance = (
        "Performance rule: when two or more dimensions are needed, use "
        "annotation.batch_create_dimensions or annotation.auto_dimension once. "
        "Do not call create_dimension_* repeatedly."
    )
    _append_tool_guidance("annotation", guidance)
    _append_tool_guidance("annotation.auto_dimension", guidance)
    _INSTALLED = True
    log.info("dimension_phase1_performance_installed", loader=str(_LOADER_PATH))


@mcp.tool(
    name="annotation.batch_create_dimensions",
    annotations={
        "title": "Batch Create Dimensions in One AutoCAD Commit",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def annotation_batch_create_dimensions(
    data: dict,
    include_screenshot: bool = True,
) -> ToolResult:
    """Create two or more dimensions in one request and one Undo group.

    Use this instead of repeatedly calling annotation.create_dimension_*.
    data.dimensions accepts linear, diameter, radius, center, and text items in
    either plan-shaped form (geometry/placement) or compact coordinate form.
    """

    return await annotation_tools._run_annotation(
        operation="batch_create_dimensions",
        data=data,
        include_image=include_screenshot,
    )
