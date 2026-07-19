"""Regression tests for the phase 2 ActiveX dimension commit engine."""

from __future__ import annotations

from pathlib import Path

from autocad_mcp import dimension_workflow
from autocad_mcp import phase2_dimension_activex
from autocad_mcp.dimension_plans import PlannedDimension


ROOT = Path(__file__).parents[1]
ACTIVEX_LISP = ROOT / "lisp-code" / "auto_dimension_activex.lsp"
LOADER_LISP = ROOT / "lisp-code" / "auto_dimension_loader.lsp"


def test_activex_engine_uses_direct_entity_creation_and_single_regen():
    text = ACTIVEX_LISP.read_text(encoding="utf-8")

    for entrypoint in (
        "vla-AddDimRotated",
        "vla-AddDimDiametric",
        "vla-AddDimRadial",
        "vla-AddLine",
        "vla-AddText",
        "vla-StartUndoMark",
        "vla-EndUndoMark",
        "vla-Regen",
    ):
        assert entrypoint in text

    for command_name in ("DIMLINEAR", "DIMDIAMETER", "DIMRADIUS", "DIMCENTER"):
        assert command_name not in text

    # JSON quotes are escaped inside AutoLISP source strings.
    assert "commit_engine" in text
    assert "activex" in text
    assert "regen_count" in text
    assert r'\"regen_count\":1' in text


def test_loader_keeps_engine_before_activex_override():
    text = LOADER_LISP.read_text(encoding="utf-8")

    engine_index = text.index('(mcp-ad-find-sibling "auto_dimension.lsp")')
    activex_index = text.index('(mcp-ad-find-sibling "auto_dimension_activex.lsp")')
    engine_load_index = text.index('(load mcp-ad-engine-path)')
    activex_load_index = text.index('(load mcp-ad-activex-path)')

    assert engine_index < activex_index
    assert engine_load_index < activex_load_index
    assert "phase3-2026-07-19" in text
    assert len(text.splitlines()) < 55


def test_center_mark_serializer_preserves_planned_size(monkeypatch):
    monkeypatch.setattr(
        dimension_workflow,
        "_dimension_to_lisp_data",
        phase2_dimension_activex._ORIGINAL_DIMENSION_TO_LISP_DATA,
    )
    monkeypatch.setattr(phase2_dimension_activex, "_INSTALLED", False)

    phase2_dimension_activex.install()
    item = PlannedDimension(
        dimension_id="D1",
        kind="center",
        geometry={"entity_id": "2F", "center": [10, 10], "size": 1.75},
        placement={"label_anchor": [10, 10]},
    )

    assert dimension_workflow._dimension_to_lisp_data(item) == '("center" "2F" 1.75)'


def test_non_center_serializer_remains_backward_compatible():
    item = PlannedDimension(
        dimension_id="D1",
        kind="linear",
        geometry={"p1": [0, 0], "p2": [100, 0]},
        placement={"base": [0, -10], "angle": 0},
    )

    assert phase2_dimension_activex._phase2_dimension_to_lisp_data(item) == (
        '("linear" (0 0 0.0) (100 0 0.0) (0 -10 0.0) 0 "")'
    )
