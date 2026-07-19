"""Regression tests for the shared FastMCP tool registration and routing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from autocad_mcp import auto_dimension_tool, phase1_dimension_perf
from autocad_mcp import phase2_dimension_activex, phase3_dimension_scope
from autocad_mcp.dimension_plans import DimensionPlanStore
from autocad_mcp import server


EXPECTED_TOOLS = {
    "drawing",
    "entity",
    "layer",
    "block",
    "annotation",
    "pid",
    "view",
    "system",
    "annotation.detect_parts",
    "annotation.plan_dimensions",
    "annotation.commit_dimension_plan",
    "annotation.auto_dimension",
    "annotation.batch_create_dimensions",
    "annotation.dimension_profiles",
    "annotation.audit_dimensions",
    "annotation.repair_dimension_layout",
}


def test_shared_registration_imports_and_installs_all_dimension_phases():
    status = server.register_optional_features()
    registered = set(server.mcp._tool_manager._tools)

    assert EXPECTED_TOOLS.issubset(registered)
    assert status == {
        "auto_dimension_tool_imported": True,
        "phase1_dimension_perf_installed": True,
        "phase2_dimension_activex_installed": True,
        "phase3_dimension_scope_installed": True,
    }
    assert phase1_dimension_perf._INSTALLED is True
    assert phase2_dimension_activex._INSTALLED is True
    assert phase3_dimension_scope._INSTALLED is True
    assert auto_dimension_tool._run_annotation is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["detect_parts", "plan_dimensions", "commit_dimension_plan", "auto_dimension", "batch_create_dimensions", "dimension_profiles", "audit_dimensions", "repair_dimension_layout"],
)
async def test_unified_annotation_routes_advanced_operations_to_run_annotation(
    operation, monkeypatch
):
    server.register_optional_features()
    calls = []

    async def fake_run_annotation(*, operation, data, include_image):
        calls.append((operation, data, include_image))
        return "routed"

    monkeypatch.setattr(auto_dimension_tool, "_run_annotation", fake_run_annotation)

    result = await server.annotation(
        operation=operation,
        data={"profile": "mechanical_mm"},
        include_screenshot=True,
    )

    assert result == "routed"
    assert calls == [(operation, {"profile": "mechanical_mm"}, True)]


def test_normalized_dimension_result_preserves_commit_metadata():
    committed = SimpleNamespace(
        commit_result={
            "backend": "file_ipc",
            "commit_engine": "activex",
            "dimensions_created": 3,
            "regen_count": 1,
            "undo_group": "single",
        },
        target={"entity_ids": ["A", "B"]},
        dimensions=[
            SimpleNamespace(kind="linear"),
            SimpleNamespace(kind="diameter"),
            SimpleNamespace(kind="center"),
        ],
    )

    result = auto_dimension_tool._normalized_dimension_commit_result(
        committed=committed,
        backend=SimpleNamespace(name="file_ipc"),
        context={
            "records": [object(), object()],
            "export_metrics": {
                "selection_scope": "handles",
                "scanned_count": 4,
                "exported_count": 2,
            },
        },
        timings={
            "export_geometry": 1.0,
            "detect_parts": 2.0,
            "build_candidates": 3.0,
            "commit": 4.0,
            "total": 5.0,
        },
    )

    assert result["created_count"] == 3
    assert result["dimension_types"] == {
        "linear": 1,
        "aligned": 0,
        "diameter": 1,
        "radius": 0,
        "angular": 0,
        "center": 1,
        "text": 0,
    }
    assert result["selection_scope"] == "handles"
    assert result["scanned_count"] == 4
    assert result["exported_count"] == 2
    assert result["commit_engine"] == "activex"
    assert result["regen_count"] == 1
    assert result["timings_ms"] == {
        "scan": 1.0,
        "detect_parts": 2.0,
        "dimension": 3.0,
        "commit": 4.0,
        "total": 5.0,
    }


@pytest.mark.asyncio
async def test_batch_engine_commits_once_without_low_level_dimension_calls(monkeypatch):
    class FakeBackend:
        name = "ezdxf"
        _doc = object()

    backend = FakeBackend()
    commit_calls = []

    async def fake_get_backend():
        return backend

    async def fake_commit_dimension_plan(backend_arg, plan, profile):
        commit_calls.append((backend_arg, plan.plan_id, profile.name))
        return {
            "backend": "ezdxf",
            "dimensions_created": len(plan.dimensions),
            "undo_group": "transactional_batch",
        }

    monkeypatch.setattr(phase1_dimension_perf, "get_backend", fake_get_backend)
    monkeypatch.setattr(
        phase1_dimension_perf,
        "commit_dimension_plan",
        fake_commit_dimension_plan,
    )
    monkeypatch.setattr(auto_dimension_tool, "_plans", DimensionPlanStore())

    result = await phase1_dimension_perf._run_batch_create(
        {
            "dimensions": [
                {
                    "kind": "linear",
                    "x1": 0,
                    "y1": 0,
                    "x2": 100,
                    "y2": 0,
                    "dim_x": 0,
                    "dim_y": -15,
                },
                {
                    "kind": "text",
                    "x": 10,
                    "y": -20,
                    "text": "NOTE",
                },
            ]
        },
        include_image=False,
    )

    assert len(commit_calls) == 1
    assert commit_calls[0][0] is backend
    assert '"created_count":2' in result
