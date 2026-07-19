"""Phase 3 scoped geometry export, safe preview cache, and export telemetry."""

from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import structlog

from autocad_mcp import auto_dimension_tool as annotation_tools
from autocad_mcp import dimension_workflow
from autocad_mcp import phase1_dimension_perf
from autocad_mcp.backends.base import CommandResult
from autocad_mcp.client import _safe, add_screenshot_if_available, get_backend
from autocad_mcp.config import LISP_DIR
from autocad_mcp.dimension_workflow import (
    build_dimension_candidates,
    commit_dimension_plan,
    drawing_fingerprint,
    geometry_only,
    records_fingerprint,
)
from autocad_mcp.part_detection import Bounds, EntityRecord, GeometrySelection, detect_parts, select_records
from autocad_mcp.server import ToolResult

log = structlog.get_logger()

_CACHE_TTL_SECONDS = 20.0
_CACHE_MAX_ENTRIES = 32
_HANDLE_PREFIX = "__MCP_SCOPE_HANDLE__:"
_REGION_PREFIX = "__MCP_SCOPE_REGION__:"

_INSTALLED = False
_DELEGATE_RUN_ANNOTATION = None
_ORIGINAL_COLLECT = None


@dataclass(frozen=True)
class _GeometryCacheEntry:
    token: str
    drawing_id: str
    dimension_layer: str
    source_layers: tuple[str, ...]
    include_dimensions: bool
    records: tuple[EntityRecord, ...]
    expires_at: float


_GEOMETRY_CACHE: dict[str, _GeometryCacheEntry] = {}


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _prune_cache(now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    for token, entry in list(_GEOMETRY_CACHE.items()):
        if entry.expires_at <= current:
            _GEOMETRY_CACHE.pop(token, None)
    while len(_GEOMETRY_CACHE) >= _CACHE_MAX_ENTRIES:
        oldest = min(_GEOMETRY_CACHE.values(), key=lambda item: item.expires_at)
        _GEOMETRY_CACHE.pop(oldest.token, None)


def _store_geometry_snapshot(
    backend: Any,
    records: Iterable[EntityRecord],
    *,
    dimension_layer: str,
    source_layers: Iterable[str],
    include_dimensions: bool = False,
) -> str:
    _prune_cache()
    token = f"dgeom_{uuid.uuid4().hex}"
    _GEOMETRY_CACHE[token] = _GeometryCacheEntry(
        token=token,
        drawing_id=drawing_fingerprint(backend),
        dimension_layer=str(dimension_layer),
        source_layers=tuple(str(layer) for layer in source_layers),
        include_dimensions=bool(include_dimensions),
        records=tuple(records),
        expires_at=time.monotonic() + _CACHE_TTL_SECONDS,
    )
    return token


def _load_geometry_snapshot(
    token: str,
    backend: Any,
    *,
    dimension_layer: str,
    source_layers: Iterable[str],
    include_dimensions: bool,
) -> list[EntityRecord]:
    _prune_cache()
    entry = _GEOMETRY_CACHE.get(token)
    if entry is None:
        raise ValueError("geometry_cache_token is unknown or expired; run annotation.detect_parts again")
    if entry.drawing_id != drawing_fingerprint(backend):
        raise ValueError("geometry_cache_token belongs to a different active drawing")
    if entry.dimension_layer != str(dimension_layer):
        raise ValueError("geometry_cache_token was created with a different dimension layer")
    if entry.source_layers != tuple(str(layer) for layer in source_layers):
        raise ValueError("geometry_cache_token was created with different source layers")
    if entry.include_dimensions != bool(include_dimensions):
        raise ValueError("geometry_cache_token is incompatible with this operation")
    return list(entry.records)


def _encode_scope(
    source_layers: Iterable[str],
    *,
    entity_ids: Iterable[str] = (),
    region: Bounds | None = None,
    region_mode: str = "intersect",
) -> str:
    tokens = [str(layer) for layer in source_layers]
    handles = [str(handle).strip() for handle in entity_ids if str(handle).strip()]
    if handles and region is not None:
        raise ValueError("entity_ids and region cannot be combined")
    if handles:
        tokens.extend(f"{_HANDLE_PREFIX}{handle}" for handle in handles)
    elif region is not None:
        mode = "contained" if region_mode == "contained" else "intersect"
        tokens.append(
            f"{_REGION_PREFIX}{mode},{region.min_x:.12g},{region.min_y:.12g},"
            f"{region.max_x:.12g},{region.max_y:.12g}"
        )
    return ";".join(tokens)


def _filter_local_records(
    records: Iterable[EntityRecord],
    *,
    entity_ids: Iterable[str] = (),
    region: Bounds | None = None,
    region_mode: str = "intersect",
) -> list[EntityRecord]:
    selected = list(records)
    handles = {str(handle).upper() for handle in entity_ids}
    if handles:
        selected = [record for record in selected if record.handle.upper() in handles]
    if region is not None:
        if region_mode == "contained":
            selected = [record for record in selected if region.contains(record.bbox)]
        else:
            selected = [record for record in selected if region.intersects(record.bbox)]
    return selected


def _expanded_record_bounds(records: Iterable[EntityRecord]) -> Bounds:
    items = list(records)
    if not items:
        raise ValueError("Cannot build a validation region without geometry")
    min_x = min(record.bbox.min_x for record in items)
    min_y = min(record.bbox.min_y for record in items)
    max_x = max(record.bbox.max_x for record in items)
    max_y = max(record.bbox.max_y for record in items)
    margin = max(max_x - min_x, max_y - min_y, 1.0) * 0.05
    return Bounds(min_x - margin, min_y - margin, max_x + margin, max_y + margin)


def _build_validation_scope(
    records: list[EntityRecord],
    selected: list[EntityRecord],
    selection: GeometrySelection,
) -> dict[str, Any]:
    if selection.entity_ids or selection.use_current_selection:
        validation_records = selected
        return {
            "mode": "handles",
            "entity_ids": [record.handle for record in selected],
            "fingerprint": records_fingerprint(validation_records),
        }
    if selection.region is not None:
        return {
            "mode": "region",
            "region": selection.region,
            "region_mode": selection.region_mode,
            "fingerprint": records_fingerprint(records),
        }
    if selection.target_part_id:
        validation_region = _expanded_record_bounds(selected)
        validation_records = _filter_local_records(records, region=validation_region)
        return {
            "mode": "region",
            "region": validation_region,
            "region_mode": "intersect",
            "fingerprint": records_fingerprint(validation_records),
        }
    return {
        "mode": "modelspace",
        "fingerprint": records_fingerprint(records),
    }


async def collect_dimension_records_scoped(
    backend: Any,
    *,
    dimension_layer: str,
    source_layers: Iterable[str] = (),
    include_dimensions: bool = False,
    use_current_selection: bool = False,
    entity_ids: Iterable[str] = (),
    region: Bounds | None = None,
    region_mode: str = "intersect",
    geometry_cache_token: str | None = None,
) -> list[EntityRecord]:
    """Collect only the requested geometry and expose exporter scan metrics."""

    source_layers = tuple(str(layer) for layer in source_layers)
    entity_ids = tuple(str(handle) for handle in entity_ids)
    if use_current_selection and (entity_ids or region is not None):
        raise ValueError("current selection cannot be combined with entity_ids or region")

    if geometry_cache_token:
        records = _load_geometry_snapshot(
            geometry_cache_token,
            backend,
            dimension_layer=dimension_layer,
            source_layers=source_layers,
            include_dimensions=include_dimensions,
        )
        metrics = {
            "cache_hit": True,
            "selection_scope": "cache_token",
            "scanned_count": 0,
            "exported_count": len(records),
            "missing_handle_count": 0,
            "elapsed_ms": 0,
        }
        setattr(backend, "_last_dimension_export_metrics", metrics)
        return records

    if backend.name == "ezdxf":
        if use_current_selection:
            raise ValueError("Current AutoCAD selection is available only with the File IPC backend")
        assert _ORIGINAL_COLLECT is not None
        records = await _ORIGINAL_COLLECT(
            backend,
            dimension_layer=dimension_layer,
            source_layers=source_layers,
            include_dimensions=include_dimensions,
            use_current_selection=False,
        )
        selected = _filter_local_records(
            records,
            entity_ids=entity_ids,
            region=region,
            region_mode=region_mode,
        )
        scope = "handles" if entity_ids else ("region" if region is not None else "modelspace")
        setattr(
            backend,
            "_last_dimension_export_metrics",
            {
                "cache_hit": False,
                "selection_scope": scope,
                "scanned_count": len(records),
                "exported_count": len(selected),
                "missing_handle_count": max(0, len(entity_ids) - len(selected)) if entity_ids else 0,
                "elapsed_ms": 0,
            },
        )
        return selected

    if backend.name != "file_ipc":
        raise RuntimeError(f"Dimension workflow is not supported by backend {backend.name!r}")

    lisp_path = (LISP_DIR / "auto_dimension.lsp").resolve()
    if not lisp_path.exists():
        raise RuntimeError(f"Automatic dimension LISP file is missing: {lisp_path}")
    report_path = Path(backend._ipc_dir) / (  # noqa: SLF001
        f"autocad_mcp_dim_geometry_{uuid.uuid4().hex[:12]}.json"
    )
    excluded_layer = "__MCP_AUDIT_INCLUDE_DIMENSIONS__" if include_dimensions else dimension_layer
    encoded_layers = _encode_scope(
        source_layers,
        entity_ids=entity_ids,
        region=region,
        region_mode=region_mode,
    )
    try:
        result = await backend.annotation_export_dimension_geometry(
            lisp_path=str(lisp_path),
            report_path=str(report_path),
            dimension_layer=excluded_layer,
            source_layers=encoded_layers,
            use_current_selection=use_current_selection,
        )
        if not result.ok:
            raise RuntimeError(result.error or "AutoCAD geometry export failed")
        if not report_path.exists():
            raise RuntimeError("AutoCAD did not produce the dimension geometry report")
        payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
        if payload.get("ok") is False:
            raise RuntimeError(str(payload.get("error", "AutoCAD geometry export failed")))
        records = [EntityRecord.from_data(item) for item in payload.get("entities", [])]
        metrics = dict(payload.get("export_metrics") or {})
        metrics.setdefault("cache_hit", False)
        metrics.setdefault("exported_count", len(records))
        setattr(backend, "_last_dimension_export_metrics", metrics)
        return records
    finally:
        try:
            report_path.unlink(missing_ok=True)
        except OSError:
            pass


async def _scoped_new_plan(data: dict[str, Any]) -> tuple[Any, list[Any], dict[str, Any]]:
    timings: dict[str, Any] = {}
    total_started = time.perf_counter()

    started = time.perf_counter()
    options = annotation_tools.AutoDimensionOptions.from_data(data)
    profile = annotation_tools._resolve_profile(data)
    selection = GeometrySelection.from_data(data)
    cache_token = str(data.get("geometry_cache_token", "")).strip() or None
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
        await collect_dimension_records_scoped(
            backend,
            dimension_layer=profile.layer,
            source_layers=options.source_layers,
            use_current_selection=selection.use_current_selection,
            entity_ids=selection.entity_ids,
            region=selection.region,
            region_mode=selection.region_mode,
            geometry_cache_token=cache_token,
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
    if selection.entity_ids:
        requested = {handle.upper() for handle in selection.entity_ids}
        resolved = {record.handle.upper() for record in selected}
        missing = sorted(requested - resolved)
        if missing:
            raise ValueError(
                "Requested entity_ids were not found or are unsupported: " + ", ".join(missing)
            )
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
    validation_scope = _build_validation_scope(records, selected, selection)
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
        "validation_scope": validation_scope,
        "phase1_timings_ms": timings,
        "export_metrics": export_metrics,
        "geometry_cache_hit": bool(export_metrics.get("cache_hit")),
    }
    log.info(
        "dimension_plan_timing",
        operation="new_plan",
        export_metrics=export_metrics,
        **timings,
    )
    return plan, selected, analysis


async def _validate_scoped_commit_context(
    backend: Any,
    context: dict[str, Any],
) -> None:
    if drawing_fingerprint(backend) != context["drawing_fingerprint"]:
        raise ValueError(
            "The active drawing changed after preview; create and approve a new dimension plan"
        )

    profile = context["profile"]
    scope = context.get("validation_scope") or {
        "mode": "handles",
        "entity_ids": [record.handle for record in context["records"]],
        "fingerprint": context["records_fingerprint"],
    }
    mode = scope["mode"]
    kwargs: dict[str, Any] = {
        "dimension_layer": profile.layer,
        "source_layers": context["source_layers"],
    }
    if mode == "handles":
        kwargs["entity_ids"] = scope["entity_ids"]
    elif mode == "region":
        kwargs["region"] = scope["region"]
        kwargs["region_mode"] = scope.get("region_mode", "intersect")
    elif mode != "modelspace":
        raise ValueError(f"Unknown dimension validation scope: {mode}")

    current = geometry_only(
        await collect_dimension_records_scoped(
            backend,
            **kwargs,
        )
    )
    if records_fingerprint(current) != scope["fingerprint"]:
        raise ValueError(
            "Selected geometry changed after preview; create and approve a new dimension plan"
        )


async def _run_scoped_detect_parts(raw: dict[str, Any], include_image: bool) -> ToolResult:
    options = annotation_tools.AutoDimensionOptions.from_data(raw)
    profile = annotation_tools._resolve_profile(raw)
    backend = await get_backend()
    records = geometry_only(
        await collect_dimension_records_scoped(
            backend,
            dimension_layer=profile.layer,
            source_layers=options.source_layers,
        )
    )
    gap_value = raw.get("gap_tolerance")
    parts = detect_parts(
        records,
        gap_tolerance=None if gap_value in (None, "") else float(gap_value),
    )
    cache_token = _store_geometry_snapshot(
        backend,
        records,
        dimension_layer=profile.layer,
        source_layers=options.source_layers,
    )
    export_metrics = copy.deepcopy(
        getattr(backend, "_last_dimension_export_metrics", {}) or {}
    )
    payload = {
        "parts": [part.to_dict() for part in parts],
        "part_count": len(parts),
        "geometry_cache_token": cache_token,
        "geometry_cache_expires_in_seconds": int(_CACHE_TTL_SECONDS),
        "export_metrics": export_metrics,
        "selection_hint": (
            "Reuse geometry_cache_token with target_part_id in plan_dimensions or "
            "auto_dimension. Region, entity_ids, and selection='current' are pushed "
            "directly into AutoCAD and avoid a full Model Space scan."
        ),
    }
    return annotation_tools._preview_result(
        payload,
        annotation_tools.render_plan_preview(records, parts=parts),
        include_image,
    )


async def _run_scoped_auto_dimension(raw: dict[str, Any], include_image: bool) -> ToolResult:
    total_started = time.perf_counter()
    plan, _records, _analysis = await annotation_tools._new_plan(raw)
    context = annotation_tools._plan_context[plan.plan_id]
    timings = dict(context.get("phase1_timings_ms", {}))
    export_metrics = copy.deepcopy(context.get("export_metrics", {}))

    started = time.perf_counter()
    backend = await get_backend()
    if context.get("geometry_cache_hit"):
        await _validate_scoped_commit_context(backend, context)
        timings["cache_revalidation"] = _elapsed_ms(started)
    else:
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
            "export_metrics": export_metrics,
            "fast_path": (
                "cached_preview_scoped_revalidation"
                if context.get("geometry_cache_hit")
                else "scoped_single_export"
            ),
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
    log.info(
        "dimension_phase3_timing",
        operation="auto_dimension",
        export_metrics=export_metrics,
        **timings,
    )
    return response


@_safe("annotation")
async def _run_phase3_operation(
    *,
    operation: str,
    data: dict | None,
    include_image: bool,
) -> ToolResult:
    raw = data or {}
    if operation == "detect_parts":
        return await _run_scoped_detect_parts(raw, include_image)
    if operation == "auto_dimension":
        return await _run_scoped_auto_dimension(raw, include_image)
    raise ValueError(f"Unsupported phase 3 annotation operation: {operation}")


async def _patched_run_annotation(
    *,
    operation: str,
    data: dict | None,
    include_image: bool,
) -> ToolResult:
    if operation in {"detect_parts", "auto_dimension"}:
        return await _run_phase3_operation(
            operation=operation,
            data=data,
            include_image=include_image,
        )
    assert _DELEGATE_RUN_ANNOTATION is not None
    return await _DELEGATE_RUN_ANNOTATION(
        operation=operation,
        data=data,
        include_image=include_image,
    )


def install() -> None:
    """Install scoped export and safe cache integration once per MCP process."""

    global _INSTALLED, _DELEGATE_RUN_ANNOTATION, _ORIGINAL_COLLECT
    if _INSTALLED:
        return

    _DELEGATE_RUN_ANNOTATION = annotation_tools._run_annotation
    _ORIGINAL_COLLECT = dimension_workflow.collect_dimension_records

    dimension_workflow.collect_dimension_records = collect_dimension_records_scoped
    annotation_tools.collect_dimension_records = collect_dimension_records_scoped
    phase1_dimension_perf.collect_dimension_records = collect_dimension_records_scoped
    annotation_tools._new_plan = _scoped_new_plan
    annotation_tools._validate_commit_context = _validate_scoped_commit_context
    annotation_tools._run_annotation = _patched_run_annotation

    guidance = (
        "Performance rule: pass region, entity_ids, or selection='current' whenever "
        "possible. After annotation.detect_parts, reuse geometry_cache_token together "
        "with target_part_id so the same full drawing is not exported twice."
    )
    phase1_dimension_perf._append_tool_guidance("annotation", guidance)
    phase1_dimension_perf._append_tool_guidance("annotation.detect_parts", guidance)
    phase1_dimension_perf._append_tool_guidance("annotation.auto_dimension", guidance)

    _INSTALLED = True
    log.info(
        "dimension_phase3_scope_installed",
        cache_ttl_seconds=_CACHE_TTL_SECONDS,
    )
