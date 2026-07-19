"""Regression tests for phase 3 scoped geometry export and cache safety."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from autocad_mcp import phase3_dimension_scope
from autocad_mcp.dimension_workflow import records_fingerprint
from autocad_mcp.part_detection import Bounds, EntityRecord


ROOT = Path(__file__).parents[1]
SCOPE_LISP = ROOT / "lisp-code" / "auto_dimension_scope.lsp"
LOADER_LISP = ROOT / "lisp-code" / "auto_dimension_loader.lsp"


def _record(handle: str = "2F") -> EntityRecord:
    return EntityRecord(
        handle=handle,
        entity_type="LINE",
        layer="0",
        bbox=Bounds(0, 0, 10, 0),
        geometry={"points": [[0, 0], [10, 0]]},
    )


def test_scope_encoding_preserves_layers_and_handles():
    encoded = phase3_dimension_scope._encode_scope(
        ["CUT", "HOLES"],
        entity_ids=["2F", "30"],
    )

    assert encoded == (
        "CUT;HOLES;"
        "__MCP_SCOPE_HANDLE__:2F;"
        "__MCP_SCOPE_HANDLE__:30"
    )


def test_scope_encoding_serializes_region_mode_and_normalized_bounds():
    encoded = phase3_dimension_scope._encode_scope(
        [],
        region=Bounds(-5, 2, 20, 40),
        region_mode="contained",
    )

    assert encoded == "__MCP_SCOPE_REGION__:contained,-5,2,20,40"


def test_scope_lisp_uses_handle_and_window_selection_with_telemetry():
    text = SCOPE_LISP.read_text(encoding="utf-8")

    assert '(handent handle)' in text
    assert '"_W"' in text
    assert '"_C"' in text
    assert '"_X"' in text
    assert 'selection_scope' in text
    assert 'scanned_count' in text
    assert 'missing_handle_count' in text


def test_phase3_loader_loads_scope_override_last():
    text = LOADER_LISP.read_text(encoding="utf-8")

    engine_index = text.index('(load mcp-ad-engine-path)')
    activex_index = text.index('(load mcp-ad-activex-path)')
    scope_index = text.index('(load mcp-ad-scope-path)')

    assert engine_index < activex_index < scope_index
    assert "phase3-2026-07-19" in text
    assert len(text.splitlines()) < 45


@pytest.mark.asyncio
async def test_commit_validation_reexports_only_selected_handles(monkeypatch):
    record = _record()
    backend = SimpleNamespace(name="ezdxf", _doc=object())
    calls = []

    async def fake_collect(_backend, **kwargs):
        calls.append(kwargs)
        return [record]

    monkeypatch.setattr(
        phase3_dimension_scope,
        "collect_dimension_records_scoped",
        fake_collect,
    )
    context = {
        "drawing_fingerprint": f"ezdxf:{id(backend._doc)}",
        "profile": SimpleNamespace(layer="MCP-DIM"),
        "source_layers": (),
        "records": [record],
        "records_fingerprint": records_fingerprint([record]),
    }

    await phase3_dimension_scope._validate_scoped_commit_context(backend, context)

    assert calls == [
        {
            "dimension_layer": "MCP-DIM",
            "source_layers": (),
            "entity_ids": ["2F"],
        }
    ]


def test_geometry_cache_rejects_different_drawing():
    backend_a = SimpleNamespace(name="ezdxf", _doc=object())
    backend_b = SimpleNamespace(name="ezdxf", _doc=object())
    token = phase3_dimension_scope._store_geometry_snapshot(
        backend_a,
        [_record()],
        dimension_layer="MCP-DIM",
        source_layers=(),
    )

    with pytest.raises(ValueError, match="different active drawing"):
        phase3_dimension_scope._load_geometry_snapshot(
            token,
            backend_b,
            dimension_layer="MCP-DIM",
            source_layers=(),
            include_dimensions=False,
        )
