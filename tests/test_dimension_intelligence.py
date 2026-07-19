"""Tests for mechanical feature recognition and dimension QA/repair."""

import json

import ezdxf

from autocad_mcp.dimension_intelligence import (
    audit_dimensions,
    recognize_mechanical_features,
    repair_dimension_layout,
)


def _drawing():
    document = ezdxf.new("R2013")
    return document, document.modelspace()


def test_recognizes_repeated_holes_concentric_geometry_and_symmetry():
    _, modelspace = _drawing()
    modelspace.add_lwpolyline([(0, 0), (100, 0), (100, 60), (0, 60)], close=True)
    modelspace.add_circle((25, 30), 5)
    modelspace.add_circle((75, 30), 5)
    modelspace.add_circle((25, 30), 9)

    report = recognize_mechanical_features(modelspace)

    hole_pattern = report.by_kind("repeated_hole_pattern")[0]
    assert hole_pattern.geometry["quantity"] == 2
    assert hole_pattern.notation == "2x ⌀10"
    assert report.by_kind("concentric_group")[0].geometry["radii"] == [5.0, 9.0]
    assert report.by_kind("symmetric_hole_pattern")


def test_recognizes_connected_slot_repeated_fillets_and_chamfer():
    _, modelspace = _drawing()
    modelspace.add_arc((10, 10), 5, 90, 270)
    modelspace.add_arc((30, 10), 5, 270, 90)
    modelspace.add_line((10, 15), (30, 15))
    modelspace.add_line((10, 5), (30, 5))
    modelspace.add_arc((60, 10), 3, 0, 90)
    modelspace.add_arc((80, 10), 3, 90, 180)
    modelspace.add_lwpolyline(
        [(0, 30), (20, 30), (25, 35), (25, 50), (0, 50)], close=True
    )

    report = recognize_mechanical_features(modelspace)

    slot = report.by_kind("slot")[0]
    assert slot.geometry["width"] == 10
    assert slot.geometry["length"] == 30
    assert report.by_kind("repeated_fillet")[0].notation == "2x R3"
    assert report.by_kind("chamfer")[0].notation == "5 x 45°"


def test_audit_finds_duplicate_layer_style_overlap_and_missing_dimensions():
    document, modelspace = _drawing()
    document.layers.add("MCP-DIM")
    document.dimstyles.duplicate_entry("Standard", "MECH")
    modelspace.add_lwpolyline([(0, 0), (100, 0), (100, 60), (0, 60)], close=True)
    modelspace.add_circle((25, 30), 5)
    first = modelspace.add_linear_dim(
        base=(0, -10), p1=(0, 0), p2=(100, 0), angle=0
    )
    first.render()
    duplicate = modelspace.add_linear_dim(
        base=(0, -10), p1=(0, 0), p2=(100, 0), angle=0
    )
    duplicate.render()

    report = audit_dimensions(
        modelspace,
        expected_layer="MCP-DIM",
        expected_style="MECH",
    )
    codes = [issue.code for issue in report.issues]

    assert "duplicate_dimension" in codes
    assert codes.count("wrong_layer") == 2
    assert codes.count("wrong_style") == 2
    assert "text_overlap" in codes
    assert "missing_overall_dimension" in codes  # vertical overall is absent
    assert "missing_hole_location" in codes


def test_repair_applies_safe_changes_and_leaves_design_intent_unresolved():
    document, modelspace = _drawing()
    document.layers.add("MCP-DIM")
    document.dimstyles.duplicate_entry("Standard", "MECH")
    modelspace.add_lwpolyline([(0, 0), (100, 0), (100, 60), (0, 60)], close=True)
    first = modelspace.add_linear_dim(base=(0, -10), p1=(0, 0), p2=(100, 0), angle=0)
    first.render()
    duplicate = modelspace.add_linear_dim(base=(0, -10), p1=(0, 0), p2=(100, 0), angle=0)
    duplicate.render()
    duplicate_handle = duplicate.dimension.dxf.handle
    report = audit_dimensions(modelspace, expected_layer="MCP-DIM", expected_style="MECH")

    result = repair_dimension_layout(modelspace, report, spacing=8)

    dimensions = [entity for entity in modelspace if entity.dxftype() == "DIMENSION"]
    assert len(dimensions) == 1
    assert dimensions[0].dxf.layer == "MCP-DIM"
    assert dimensions[0].dxf.dimstyle == "MECH"
    assert duplicate_handle not in {entity.dxf.handle for entity in dimensions}
    unresolved_codes = {
        issue.code for issue in report.issues if issue.issue_id in result.unresolved_issue_ids
    }
    assert "missing_overall_dimension" in unresolved_codes


def test_repair_can_return_a_dry_run_without_mutation():
    _, modelspace = _drawing()
    modelspace.add_line((0, 0), (20, 0))
    dimension = modelspace.add_linear_dim(base=(0, -5), p1=(0, 0), p2=(20, 0), angle=0)
    dimension.render()
    report = audit_dimensions(modelspace, expected_layer="MCP-DIM")

    result = repair_dimension_layout(modelspace, report, apply=False)

    assert result.applied is False
    assert result.actions
    assert dimension.dimension.dxf.layer == "0"


def test_json_records_support_feature_audit_and_repair_planning():
    records = [
        {
            "type": "LWPOLYLINE",
            "handle": "P1",
            "points": [[0, 0], [40, 0], [40, 20], [0, 20]],
            "closed": True,
        },
        {"type": "CIRCLE", "handle": "C1", "center": [10, 10], "radius": 2},
        {"type": "CIRCLE", "handle": "C2", "center": [30, 10], "radius": 2},
        {
            "type": "DIMENSION",
            "handle": "D1",
            "layer": "0",
            "dimstyle": "Standard",
            "dimtype": 0,
            "defpoint": [0, -5],
            "defpoint2": [0, 0],
            "defpoint3": [40, 0],
            "text_midpoint": [20, -5],
            "angle": 0,
            "measurement": 40,
        },
    ]

    features = recognize_mechanical_features(records)
    audit = audit_dimensions(records, expected_layer="MCP-DIM")
    repair = repair_dimension_layout(records, audit, apply=False)

    assert features.by_kind("repeated_hole_pattern")[0].entity_handles == ("C1", "C2")
    assert any(issue.code == "wrong_layer" for issue in audit.issues)
    assert repair.actions[0]["action"] == "set_layer"
    json.dumps(features.to_dict())
    json.dumps(audit.to_dict())
    json.dumps(repair.to_dict())


def test_json_line_record_accepts_exported_points_shape():
    report = recognize_mechanical_features(
        [{"type": "LINE", "handle": "L1", "points": [[0, 0], [10, 0]]}]
    )

    assert report.features == ()


def test_audit_detects_detached_references_and_chain_accumulation_error():
    _, modelspace = _drawing()
    modelspace.add_lwpolyline([(0, 0), (100, 0), (100, 40), (0, 40)], close=True)
    left = modelspace.add_linear_dim(base=(0, -8), p1=(0, 0), p2=(50, 0), angle=0)
    left.set_text("49.9")
    left.render()
    right = modelspace.add_linear_dim(base=(50, -8), p1=(50, 0), p2=(100, 0), angle=0)
    right.set_text("49.9")
    right.render()
    overall = modelspace.add_linear_dim(base=(0, -16), p1=(0, 0), p2=(100, 0), angle=0)
    overall.set_text("100")
    overall.render()
    detached = modelspace.add_linear_dim(
        base=(200, 190), p1=(200, 200), p2=(220, 200), angle=0
    )
    detached.render()

    report = audit_dimensions(modelspace)
    codes = {issue.code for issue in report.issues}

    assert "chain_accumulation_error" in codes
    assert "detached_geometry_reference" in codes
