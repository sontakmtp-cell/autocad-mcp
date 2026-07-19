"""Regression tests for phase 1 dimension performance helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from autocad_mcp.phase1_dimension_perf import normalize_batch_dimensions


def test_normalize_compact_linear_batch_dimension():
    result = normalize_batch_dimensions(
        [
            {
                "kind": "linear",
                "x1": 0,
                "y1": 0,
                "x2": 100,
                "y2": 0,
                "dim_x": 0,
                "dim_y": -15,
            }
        ]
    )

    assert result == [
        {
            "kind": "linear",
            "geometry": {"p1": [0.0, 0.0], "p2": [100.0, 0.0]},
            "placement": {
                "base": [0.0, -15.0],
                "angle": 0.0,
                "label_anchor": [0.0, -15.0],
            },
            "metadata": {},
        }
    ]


def test_normalize_plan_shaped_radius_dimension():
    result = normalize_batch_dimensions(
        [
            {
                "kind": "radius",
                "geometry": {"entity_id": "2F", "point": [12, 18]},
                "placement": {"label_anchor": [12, 18]},
                "text": "R<>",
            }
        ]
    )

    assert result[0]["geometry"] == {"entity_id": "2F", "point": [12.0, 18.0]}
    assert result[0]["text"] == "R<>"


def test_batch_requires_non_empty_dimensions():
    with pytest.raises(ValueError, match="non-empty array"):
        normalize_batch_dimensions([])


def test_loader_is_small_and_version_aware():
    loader = Path(__file__).parents[1] / "lisp-code" / "auto_dimension_loader.lsp"
    text = loader.read_text(encoding="utf-8")

    assert "*mcp-auto-dimension-loader-version*" in text
    assert '(mcp-ad-find-sibling "auto_dimension.lsp")' in text
    assert "mcp-ad-loader-path" in text
    assert len(text.splitlines()) < 55
