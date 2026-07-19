"""MCP registration for part-aware, preview-first mechanical dimensioning."""

from __future__ import annotations

import io
import os
import uuid
from pathlib import Path
from typing import Any

import ezdxf
from mcp.types import ImageContent, TextContent

from autocad_mcp.autodim import AutoDimensionOptions
from autocad_mcp.backends.base import CommandResult
from autocad_mcp.client import _json, _safe, add_screenshot_if_available, get_backend
from autocad_mcp.dimension_intelligence import (
    audit_dimensions as run_dimension_audit,
    repair_dimension_layout as run_dimension_repair,
)
from autocad_mcp.dimension_plans import DimensionPlanStore
from autocad_mcp.dimension_profiles import DimensionProfile, DimensionProfileStore
from autocad_mcp.dimension_workflow import (
    apply_file_ipc_repairs,
    build_dimension_candidates,
    collect_dimension_records,
    commit_dimension_plan,
    drawing_fingerprint,
    geometry_only,
    records_fingerprint,
    records_for_intelligence,
    render_audit_preview,
    render_plan_preview,
)
from autocad_mcp.part_detection import GeometrySelection, detect_parts, select_records
from autocad_mcp.server import ToolResult, mcp


def _profile_path() -> Path | None:
    configured = os.environ.get("AUTOCAD_MCP_DIMENSION_PROFILES", "").strip()
    if configured:
        return Path(configured)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "autocad-mcp" / "dimension_profiles.json"
    return None


_profiles = DimensionProfileStore(_profile_path())
_plans = DimensionPlanStore()
_plan_context: dict[str, dict[str, Any]] = {}
_audit_context: dict[str, dict[str, Any]] = {}


def _boolean(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"Expected a boolean value, got {value!r}")


def _resolve_profile(data: dict[str, Any]) -> DimensionProfile:
    name = str(data.get("profile", data.get("profile_name", "mechanical_mm"))).strip()
    overrides = data.get("profile_overrides")
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError("profile_overrides must be an object")
    resolved_overrides = dict(overrides or {})
    if data.get("dimension_layer") not in (None, ""):
        resolved_overrides["layer"] = data["dimension_layer"]
    return _profiles.get(name, resolved_overrides or None)


def _preview_result(payload: dict[str, Any], image_data: str, enabled: bool) -> ToolResult:
    if not enabled:
        return _json({"ok": True, "payload": payload})
    return [
        TextContent(type="text", text=_json({"ok": True, "payload": payload})),
        ImageContent(type="image", data=image_data, mimeType="image/png"),
    ]


def _dimension_type_counts(dimensions: list[Any]) -> dict[str, int]:
    counts = {
        "linear": 0,
        "aligned": 0,
        "diameter": 0,
        "radius": 0,
        "angular": 0,
        "center": 0,
        "text": 0,
    }
    aliases = {
        "dimension_linear": "linear",
        "dimension_aligned": "aligned",
        "dimension_angular": "angular",
        "dimension_diameter": "diameter",
        "dimension_radius": "radius",
        "center_mark": "center",
        "create_text": "text",
    }
    for dimension in dimensions:
        kind = str(getattr(dimension, "kind", "")).strip().lower()
        kind = aliases.get(kind, kind)
        if kind in counts:
            counts[kind] += 1
    return counts


def _normalized_dimension_commit_result(
    *,
    committed: Any,
    backend: Any,
    context: dict[str, Any] | None = None,
    timings: dict[str, Any] | None = None,
    manual_batch: bool = False,
) -> dict[str, Any]:
    """Expose one stable result shape while retaining backend metadata."""

    commit_result = dict(committed.commit_result or {})
    target = dict(committed.target or {})
    export_metrics = dict((context or {}).get("export_metrics") or {})
    phase_timings = dict(timings or (context or {}).get("phase1_timings_ms") or {})

    if export_metrics.get("selection_scope"):
        selection_scope = str(export_metrics["selection_scope"])
    elif manual_batch or target.get("manual_batch"):
        selection_scope = "manual_batch"
    elif target.get("use_current_selection"):
        selection_scope = "current_selection"
    elif target.get("entity_ids"):
        selection_scope = "entity_ids"
    elif target.get("region"):
        selection_scope = "region"
    elif target.get("target_part_id"):
        selection_scope = "target_part"
    else:
        selection_scope = "modelspace"

    dimensions = list(committed.dimensions)
    commit_time = phase_timings.get("commit", 0)
    total_time = phase_timings.get(
        "total",
        phase_timings.get("plan_total", phase_timings.get("server_before_screenshot", 0)),
    )
    scan_time = phase_timings.get(
        "scan",
        phase_timings.get("export_geometry", export_metrics.get("elapsed_ms", 0)),
    )
    dimension_time = phase_timings.get(
        "dimension",
        phase_timings.get(
            "build_candidates",
            phase_timings.get("normalize_batch", 0),
        ),
    )

    return {
        **commit_result,
        "created_count": int(commit_result.get("dimensions_created", len(dimensions))),
        "dimension_types": _dimension_type_counts(dimensions),
        "selection_scope": selection_scope,
        "scanned_count": int(export_metrics.get("scanned_count", 0) or 0),
        "exported_count": int(
            export_metrics.get("exported_count", len((context or {}).get("records", ())))
            or 0
        ),
        "commit_engine": str(
            commit_result.get("commit_engine")
            or commit_result.get("backend")
            or getattr(backend, "name", "unknown")
        ),
        "regen_count": int(commit_result.get("regen_count", 0) or 0),
        "timings_ms": {
            "scan": scan_time,
            "detect_parts": phase_timings.get("detect_parts", 0),
            "dimension": dimension_time,
            "commit": commit_time,
            "total": total_time,
        },
    }


async def _new_plan(data: dict[str, Any]) -> tuple[Any, list[Any], dict[str, Any]]:
    options = AutoDimensionOptions.from_data(data)
    profile = _resolve_profile(data)
    selection = GeometrySelection.from_data(data)
    if options.clear_existing and selection.is_active:
        raise ValueError(
            "clear_existing cannot be combined with a part/region/entity selector; "
            "use audit_dimensions and repair_dimension_layout to avoid deleting another part"
        )
    backend = await get_backend()
    records = geometry_only(
        await collect_dimension_records(
            backend,
            dimension_layer=profile.layer,
            source_layers=options.source_layers,
            use_current_selection=selection.use_current_selection,
        )
    )
    gap_value = data.get("part_gap_tolerance")
    parts = detect_parts(
        records,
        gap_tolerance=None if gap_value in (None, "") else float(gap_value),
    )
    selected = select_records(records, selection, parts=parts)
    if not selected:
        raise ValueError("The requested target contains no supported geometry")
    candidates, analysis = build_dimension_candidates(
        selected,
        options=options,
        profile=profile,
    )
    target = {
        "target_part_id": selection.target_part_id,
        "region": selection.region.to_dict() if selection.region else None,
        "entity_ids": list(selection.entity_ids),
        "use_current_selection": selection.use_current_selection,
        "selected_entity_ids": [record.handle for record in selected],
        "clear_existing": options.clear_existing,
        "analysis": analysis,
    }
    plan = _plans.create(candidates, profile_name=profile.name, target=target)
    active_plan_ids = _plans.plan_ids()
    for stale_plan_id in set(_plan_context) - active_plan_ids:
        _plan_context.pop(stale_plan_id, None)
    _plan_context[plan.plan_id] = {
        "records": selected,
        "selection": selection,
        "profile": profile,
        "source_layers": options.source_layers,
        "drawing_fingerprint": drawing_fingerprint(backend),
        "records_fingerprint": records_fingerprint(selected),
    }
    return plan, selected, analysis


async def _validate_commit_context(
    backend: Any,
    context: dict[str, Any],
) -> None:
    if drawing_fingerprint(backend) != context["drawing_fingerprint"]:
        raise ValueError(
            "The active drawing changed after preview; create and approve a new dimension plan"
        )
    profile = context["profile"]
    current = geometry_only(
        await collect_dimension_records(
            backend,
            dimension_layer=profile.layer,
            source_layers=context["source_layers"],
        )
    )
    if context["selection"].use_current_selection:
        selected = select_records(
            current,
            {"entity_ids": [record.handle for record in context["records"]]},
        )
    else:
        current_parts = detect_parts(current)
        selected = select_records(current, context["selection"], parts=current_parts)
    if records_fingerprint(selected) != context["records_fingerprint"]:
        raise ValueError(
            "Selected geometry changed after preview; create and approve a new dimension plan"
        )


@_safe("annotation")
async def _run_annotation(
    *,
    operation: str,
    data: dict | None,
    include_image: bool,
) -> ToolResult:
    raw = data or {}

    if operation == "detect_parts":
        options = AutoDimensionOptions.from_data(raw)
        profile = _resolve_profile(raw)
        backend = await get_backend()
        records = geometry_only(
            await collect_dimension_records(
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
        payload = {
            "parts": [part.to_dict() for part in parts],
            "part_count": len(parts),
            "selection_hint": (
                "Use target_part_id, region, entity_ids, or selection='current' "
                "with plan_dimensions/auto_dimension."
            ),
        }
        return _preview_result(
            payload,
            render_plan_preview(records, parts=parts),
            include_image,
        )

    if operation == "plan_dimensions":
        existing_plan_id = str(raw.get("plan_id", "")).strip()
        if existing_plan_id:
            if _boolean(raw.get("discard")):
                _plans.discard(existing_plan_id)
                _plan_context.pop(existing_plan_id, None)
                return _json(
                    {"ok": True, "payload": {"plan_id": existing_plan_id, "discarded": True}}
                )
            plan = _plans.revise(
                existing_plan_id,
                expected_revision=int(raw.get("expected_revision", raw.get("revision", 0))),
                remove_ids=raw.get("remove_dimension_ids", raw.get("remove_ids", [])),
                placement_overrides=raw.get("placement_overrides", {}),
                add_candidates=raw.get("add_dimensions", []),
            )
            context = _plan_context.get(plan.plan_id)
            if context is None:
                raise ValueError("Dimension plan context is no longer available; create a new plan")
            records = context["records"]
        else:
            plan, records, _ = await _new_plan(raw)
        return _preview_result(
            plan.to_dict(),
            render_plan_preview(records, plan),
            include_image,
        )

    if operation == "commit_dimension_plan":
        plan_id = str(raw.get("plan_id", "")).strip()
        if not plan_id:
            raise ValueError("plan_id is required")
        context = _plan_context.get(plan_id)
        if context is None:
            raise ValueError("Dimension plan context is no longer available; create a new plan")
        backend = await get_backend()
        expected_revision_raw = raw.get("expected_revision", raw.get("revision"))
        if expected_revision_raw is None:
            raise ValueError("expected_revision is required when committing a plan")
        expected_revision = int(expected_revision_raw)
        current_plan = _plans.get(plan_id)
        if current_plan.revision != expected_revision:
            raise ValueError(
                f"Plan revision is {current_plan.revision}, not expected revision {expected_revision}"
            )
        if current_plan.status == "draft":
            await _validate_commit_context(backend, context)

        async def executor(plan):
            return await commit_dimension_plan(backend, plan, context["profile"])

        plan = await _plans.commit(
            plan_id,
            executor,
            expected_revision=expected_revision,
        )
        result = CommandResult(ok=True, payload=plan.to_dict())
        return await add_screenshot_if_available(result, include_image)

    if operation == "auto_dimension":
        plan, _records, _analysis = await _new_plan(raw)
        context = _plan_context[plan.plan_id]
        backend = await get_backend()
        await _validate_commit_context(backend, context)

        async def executor(plan_to_commit):
            return await commit_dimension_plan(backend, plan_to_commit, context["profile"])

        committed = await _plans.commit(
            plan.plan_id,
            executor,
            expected_revision=plan.revision,
        )
        result = CommandResult(
            ok=True,
            payload={
                "plan_id": committed.plan_id,
                "profile_name": committed.profile_name,
                "target": committed.target,
                "dimensions": [item.to_dict() for item in committed.dimensions],
                **(committed.commit_result or {}),
                **_normalized_dimension_commit_result(
                    committed=committed,
                    backend=backend,
                    context=context,
                ),
            },
        )
        return await add_screenshot_if_available(result, include_image)

    if operation == "dimension_profiles":
        action = str(raw.get("action", "list")).strip().lower()
        if action == "list":
            profiles = [profile.to_dict() for profile in _profiles.list_profiles()]
            return _json({"ok": True, "payload": {"profiles": profiles}})
        if action == "get":
            profile = _profiles.get(str(raw.get("name", "")))
            return _json({"ok": True, "payload": profile.to_dict()})
        if action == "save":
            profile_data = raw.get("profile")
            if not isinstance(profile_data, dict):
                raise ValueError("data.profile must be a profile object")
            profile = _profiles.save(
                profile_data,
                replace_existing=_boolean(raw.get("replace_existing")),
            )
            return _json({"ok": True, "payload": profile.to_dict()})
        if action == "delete":
            deleted = _profiles.delete(str(raw.get("name", "")))
            return _json({"ok": True, "payload": {"deleted": deleted}})
        raise ValueError("action must be list, get, save, or delete")

    if operation == "audit_dimensions":
        profile = _resolve_profile(raw)
        backend = await get_backend()
        records = await collect_dimension_records(
            backend,
            dimension_layer=profile.layer,
            include_dimensions=True,
        )
        source = (
            list(backend._msp)  # noqa: SLF001
            if backend.name == "ezdxf"
            else records_for_intelligence(records)
        )
        audit = run_dimension_audit(
            source,
            expected_layer=profile.layer,
            expected_style=profile.dimstyle,
            text_height=profile.text_height,
        )
        audit_id = f"daudit_{uuid.uuid4().hex}"
        if len(_audit_context) >= 128:
            _audit_context.pop(next(iter(_audit_context)))
        _audit_context[audit_id] = {
            "audit": audit,
            "records": records,
            "profile": profile,
            "drawing_fingerprint": drawing_fingerprint(backend),
            "records_fingerprint": records_fingerprint(records),
        }
        payload = {"audit_id": audit_id, **audit.to_dict()}
        return _preview_result(
            payload,
            render_audit_preview(records, audit),
            include_image,
        )

    if operation == "repair_dimension_layout":
        audit_id = str(raw.get("audit_id", "")).strip()
        if not audit_id:
            raise ValueError("audit_id from annotation.audit_dimensions is required")
        context = _audit_context.get(audit_id)
        if context is None:
            raise ValueError("Unknown or expired audit_id; run audit_dimensions again")
        backend = await get_backend()
        profile = context["profile"]
        records = await collect_dimension_records(
            backend,
            dimension_layer=profile.layer,
            include_dimensions=True,
        )
        if (
            drawing_fingerprint(backend) != context["drawing_fingerprint"]
            or records_fingerprint(records) != context["records_fingerprint"]
        ):
            raise ValueError(
                "The drawing changed after audit; run audit_dimensions again before repair"
            )
        audit = context["audit"]
        source = (
            list(backend._msp)  # noqa: SLF001
            if backend.name == "ezdxf"
            else records_for_intelligence(records)
        )
        issue_ids = raw.get("issue_ids")
        repair_target = backend._msp if backend.name == "ezdxf" else source  # noqa: SLF001
        if backend.name == "ezdxf":
            snapshot_stream = io.StringIO()
            backend._doc.write(snapshot_stream)  # noqa: SLF001
            snapshot_stream.seek(0)
            try:
                if profile.layer not in backend._doc.layers:  # noqa: SLF001
                    backend._doc.layers.add(profile.layer, color=2)  # noqa: SLF001
                if profile.dimstyle not in backend._doc.dimstyles:  # noqa: SLF001
                    backend._doc.dimstyles.duplicate_entry(  # noqa: SLF001
                        "Standard", profile.dimstyle
                    )
                repair = run_dimension_repair(
                    repair_target,
                    audit,
                    issue_ids=issue_ids,
                    spacing=raw.get("spacing"),
                    apply=True,
                )
            except BaseException:
                restored = ezdxf.read(snapshot_stream)
                backend._doc = restored  # noqa: SLF001
                backend._msp = restored.modelspace()  # noqa: SLF001
                backend._screenshot.doc = restored  # noqa: SLF001
                raise
        else:
            repair = run_dimension_repair(
                repair_target,
                audit,
                issue_ids=issue_ids,
                spacing=raw.get("spacing"),
                apply=False,
            )
        payload = repair.to_dict()
        if backend.name == "file_ipc":
            applied = await apply_file_ipc_repairs(backend, repair.actions, profile)
            payload["applied"] = True
            payload["commit_result"] = applied
        _audit_context.pop(audit_id, None)
        result = CommandResult(ok=True, payload=payload)
        return await add_screenshot_if_available(result, include_image)

    raise ValueError(f"Unknown annotation operation: {operation}")


@mcp.tool(
    name="annotation.detect_parts",
    annotations={
        "title": "Detect Independent Drawing Parts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def annotation_detect_parts(
    data: dict | None = None,
    include_preview: bool = True,
) -> ToolResult:
    """Cluster Model Space geometry and return part_1, part_2... with an indexed preview."""

    return await _run_annotation(
        operation="detect_parts",
        data=data,
        include_image=include_preview,
    )


@mcp.tool(
    name="annotation.plan_dimensions",
    annotations={
        "title": "Plan Dimensions Without Editing the Drawing",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def annotation_plan_dimensions(
    data: dict | None = None,
    include_preview: bool = True,
) -> ToolResult:
    """Create or revise a D1/D2... plan; no AutoCAD entity is created."""

    return await _run_annotation(
        operation="plan_dimensions",
        data=data,
        include_image=include_preview,
    )


@mcp.tool(
    name="annotation.commit_dimension_plan",
    annotations={
        "title": "Commit an Approved Dimension Plan",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def annotation_commit_dimension_plan(
    data: dict,
    include_screenshot: bool = True,
) -> ToolResult:
    """Commit one approved plan exactly once; File IPC uses one AutoCAD UNDO group."""

    return await _run_annotation(
        operation="commit_dimension_plan",
        data=data,
        include_image=include_screenshot,
    )


@mcp.tool(
    name="annotation.auto_dimension",
    annotations={
        "title": "Part-aware Automatic Mechanical Dimensioning",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def annotation_auto_dimension(
    data: dict | None = None,
    include_screenshot: bool = True,
) -> ToolResult:
    """Plan and immediately commit dimensions for one selected part/region/entity set."""

    return await _run_annotation(
        operation="auto_dimension",
        data=data,
        include_image=include_screenshot,
    )


@mcp.tool(
    name="annotation.dimension_profiles",
    annotations={
        "title": "Manage Reusable Dimension Profiles",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def annotation_dimension_profiles(data: dict | None = None) -> ToolResult:
    """List/get/save/delete mechanical_mm, mechanical_inch, iso_simple or custom profiles."""

    return await _run_annotation(
        operation="dimension_profiles",
        data=data,
        include_image=False,
    )


@mcp.tool(
    name="annotation.audit_dimensions",
    annotations={
        "title": "Audit Dimension Quality",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def annotation_audit_dimensions(
    data: dict | None = None,
    include_preview: bool = True,
) -> ToolResult:
    """Find duplicates, overlap, crossings, missing intent, detached refs and style errors."""

    return await _run_annotation(
        operation="audit_dimensions",
        data=data,
        include_image=include_preview,
    )


@mcp.tool(
    name="annotation.repair_dimension_layout",
    annotations={
        "title": "Repair Deterministic Dimension Layout Issues",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def annotation_repair_dimension_layout(
    data: dict | None = None,
    include_screenshot: bool = True,
) -> ToolResult:
    """Remove duplicates and fix safe layer/style/lane issues from a fresh audit."""

    return await _run_annotation(
        operation="repair_dimension_layout",
        data=data,
        include_image=include_screenshot,
    )
