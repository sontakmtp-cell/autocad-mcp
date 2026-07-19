"""Tests for the one-call automatic dimensioning feature."""

import pytest

from autocad_mcp.autodim import AutoDimensionOptions, run_ezdxf_auto_dimension
from autocad_mcp.backends.ezdxf_backend import EzdxfBackend


def test_options_defaults_and_validation():
    options = AutoDimensionOptions.from_data(None)
    assert options.mode == "balanced"
    assert options.include_holes is True
    assert options.dimension_layer == "MCP-DIM"

    with pytest.raises(ValueError, match="mode must be"):
        AutoDimensionOptions.from_data({"mode": "everything"})

    with pytest.raises(ValueError, match="spacing"):
        AutoDimensionOptions.from_data({"spacing": 0})

    assert AutoDimensionOptions.from_data({"include_holes": "false"}).include_holes is False
    with pytest.raises(ValueError, match="boolean"):
        AutoDimensionOptions.from_data({"include_holes": "sometimes"})


@pytest.mark.asyncio
async def test_ezdxf_auto_dimension_detects_holes_and_symmetry():
    backend = EzdxfBackend()
    await backend.initialize()
    await backend.create_rectangle(0, 0, 100, 60)
    await backend.create_circle(25, 30, 5)
    await backend.create_circle(75, 30, 5)
    await backend.create_arc(50, 30, 12, 0, 90)

    result = await run_ezdxf_auto_dimension(
        backend,
        AutoDimensionOptions.from_data({"mode": "balanced"}),
    )

    assert result.ok is True
    assert result.payload["dimensions_created"] >= 5
    assert result.payload["hole_dimensions"] == 2
    assert result.payload["arc_dimensions"] == 1
    assert result.payload["vertical_symmetry_pairs"] >= 1
    assert result.payload["dimension_layer"] == "MCP-DIM"


@pytest.mark.asyncio
async def test_ezdxf_auto_dimension_can_clear_its_own_layer():
    backend = EzdxfBackend()
    await backend.initialize()
    await backend.create_rectangle(0, 0, 40, 20)

    first = await run_ezdxf_auto_dimension(
        backend,
        AutoDimensionOptions.from_data({"mode": "minimal"}),
    )
    assert first.ok is True

    second = await run_ezdxf_auto_dimension(
        backend,
        AutoDimensionOptions.from_data(
            {"mode": "minimal", "clear_existing": True}
        ),
    )
    assert second.ok is True
    generated = [
        entity
        for entity in backend._msp
        if entity.dxf.get("layer", "0") == "MCP-DIM"
    ]
    dimensions = [entity for entity in generated if entity.dxftype() == "DIMENSION"]
    assert len(dimensions) == second.payload["dimensions_created"]
    assert len(generated) == second.payload["dimensions_created"]
