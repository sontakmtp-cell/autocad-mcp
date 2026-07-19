"""One-call automatic dimensioning for AutoCAD File IPC and ezdxf backends."""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from autocad_mcp.backends.base import CommandResult
from autocad_mcp.config import LISP_DIR

_VALID_MODES = frozenset({"minimal", "balanced", "detailed"})


def _validate_layer_name(value: object, field: str) -> str:
    layer = str(value).strip()
    if not layer:
        raise ValueError(f"{field} cannot be empty")
    if any(ord(char) < 32 for char in layer):
        raise ValueError(f"{field} contains control characters")
    if any(char in layer for char in '<>/\\":;?*|=,`'):
        raise ValueError(f"{field} contains characters AutoCAD does not allow")
    return layer


def _as_bool(value: Any, default: bool) -> bool:
    """Accept JSON booleans and common string forms without truthiness surprises."""
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


@dataclass(frozen=True)
class AutoDimensionOptions:
    """Normalized options shared by both automatic-dimension backends."""

    mode: str = "balanced"
    include_overall: bool = True
    include_features: bool = True
    include_holes: bool = True
    include_arcs: bool = True
    include_centers: bool = True
    detect_symmetry: bool = True
    clear_existing: bool = False
    zoom_preview: bool = True
    dimension_layer: str = "MCP-DIM"
    spacing: float | None = None
    source_layers: tuple[str, ...] = ()

    @classmethod
    def from_data(cls, data: dict[str, Any] | None) -> "AutoDimensionOptions":
        raw = data or {}
        mode = str(raw.get("mode", "balanced")).strip().lower()
        if mode not in _VALID_MODES:
            supported = ", ".join(sorted(_VALID_MODES))
            raise ValueError(f"mode must be one of: {supported}")

        layer = _validate_layer_name(
            raw.get("dimension_layer", "MCP-DIM"),
            "dimension_layer",
        )

        spacing_raw = raw.get("spacing")
        spacing = None if spacing_raw in (None, "") else float(spacing_raw)
        if spacing is not None and spacing <= 0:
            raise ValueError("spacing must be greater than zero")

        source_layers_raw = raw.get("source_layers") or []
        if not isinstance(source_layers_raw, (list, tuple)):
            raise ValueError("source_layers must be an array of layer names")
        source_layers = tuple(
            _validate_layer_name(item, "source_layers item")
            for item in source_layers_raw
            if str(item).strip()
        )

        return cls(
            mode=mode,
            include_overall=_as_bool(raw.get("include_overall"), True),
            include_features=_as_bool(raw.get("include_features"), True),
            include_holes=_as_bool(raw.get("include_holes"), True),
            include_arcs=_as_bool(raw.get("include_arcs"), True),
            include_centers=_as_bool(raw.get("include_centers"), True),
            detect_symmetry=_as_bool(raw.get("detect_symmetry"), True),
            clear_existing=_as_bool(raw.get("clear_existing"), False),
            zoom_preview=_as_bool(raw.get("zoom_preview"), True),
            dimension_layer=layer,
            spacing=spacing,
            source_layers=source_layers,
        )


def _lisp_string(value: str) -> str:
    return '"' + value.replace("\\", "/").replace('"', '\\"') + '"'


def _lisp_bool(value: bool) -> str:
    return "T" if value else "nil"


def _lisp_string_list(values: Iterable[str]) -> str:
    items = " ".join(_lisp_string(value) for value in values)
    return f"'({items})" if items else "nil"


async def run_file_ipc_auto_dimension(
    backend: Any,
    options: AutoDimensionOptions,
) -> CommandResult:
    """Run the local AutoLISP auto-dimension engine in a single IPC command."""

    lisp_path = (LISP_DIR / "auto_dimension.lsp").resolve()
    if not lisp_path.exists():
        return CommandResult(
            ok=False,
            error=f"Automatic dimension LISP file is missing: {lisp_path}",
        )

    report_path = Path(backend._ipc_dir) / (  # noqa: SLF001 - backend extension point
        f"autocad_mcp_autodim_{uuid.uuid4().hex[:12]}.json"
    )
    spacing = options.spacing if options.spacing is not None else 0.0
    script = "\n".join(
        [
            f"(load {_lisp_string(str(lisp_path))})",
            "(mcp-auto-dimension",
            f"  {_lisp_string(options.mode)}",
            f"  {_lisp_bool(options.include_overall)}",
            f"  {_lisp_bool(options.include_features)}",
            f"  {_lisp_bool(options.include_holes)}",
            f"  {_lisp_bool(options.include_arcs)}",
            f"  {_lisp_bool(options.include_centers)}",
            f"  {_lisp_bool(options.detect_symmetry)}",
            f"  {_lisp_bool(options.clear_existing)}",
            f"  {_lisp_bool(options.zoom_preview)}",
            f"  {_lisp_string(options.dimension_layer)}",
            f"  {spacing:.12g}",
            f"  {_lisp_string_list(options.source_layers)}",
            f"  {_lisp_string(str(report_path))}",
            ")",
        ]
    )

    try:
        execute_result = await backend.execute_lisp(script)
        if not execute_result.ok:
            return execute_result
        if not report_path.exists():
            return CommandResult(
                ok=False,
                error=(
                    "AutoCAD completed the LISP call but did not produce the "
                    "automatic-dimension report. Reload mcp_dispatch.lsp and retry."
                ),
            )
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            report = json.loads(report_path.read_text(encoding="cp1252"))
        if report.get("ok") is False:
            return CommandResult(ok=False, error=str(report.get("error", "Auto-dimension failed")))
        return CommandResult(ok=True, payload=report)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return CommandResult(ok=False, error=f"Could not read automatic-dimension report: {exc}")
    finally:
        try:
            report_path.unlink(missing_ok=True)
        except OSError:
            pass


def _unique_sorted(values: Iterable[float], tolerance: float) -> list[float]:
    ordered = sorted(float(value) for value in values)
    result: list[float] = []
    for value in ordered:
        if not result or abs(value - result[-1]) > tolerance:
            result.append(value)
    return result


def _thin_coordinates(values: list[float], cap: int) -> list[float]:
    if len(values) <= cap:
        return values
    if cap <= 2:
        return [values[0], values[-1]]
    selected = [values[0]]
    span = len(values) - 1
    for index in range(1, cap - 1):
        selected.append(values[round(index * span / (cap - 1))])
    selected.append(values[-1])
    return _unique_sorted(selected, 1e-9)


def _entity_points(entity: Any) -> list[tuple[float, float]]:
    entity_type = entity.dxftype()
    if entity_type == "LINE":
        return [
            (float(entity.dxf.start.x), float(entity.dxf.start.y)),
            (float(entity.dxf.end.x), float(entity.dxf.end.y)),
        ]
    if entity_type == "LWPOLYLINE":
        return [(float(x), float(y)) for x, y in entity.get_points("xy")]
    if entity_type == "POLYLINE":
        return [
            (float(vertex.dxf.location.x), float(vertex.dxf.location.y))
            for vertex in entity.vertices
        ]
    return []


def _add_linear_dimension(
    modelspace: Any,
    *,
    p1: tuple[float, float],
    p2: tuple[float, float],
    base: tuple[float, float],
    angle: float,
    layer: str,
) -> bool:
    try:
        dim = modelspace.add_linear_dim(
            base=base,
            p1=p1,
            p2=p2,
            angle=angle,
            dxfattribs={"layer": layer},
        )
        dim.render()
        return True
    except Exception:
        return False


def _add_radius_dimension(
    modelspace: Any,
    *,
    center: tuple[float, float],
    point: tuple[float, float],
    layer: str,
    diameter: bool,
) -> bool:
    try:
        if diameter:
            dim = modelspace.add_diameter_dim(
                center=center,
                mpoint=point,
                dxfattribs={"layer": layer},
            )
        else:
            dim = modelspace.add_radius_dim(
                center=center,
                mpoint=point,
                dxfattribs={"layer": layer},
            )
        dim.render()
        return True
    except Exception:
        return False


def _find_symmetric_pairs(
    circles: list[dict[str, Any]],
    center_x: float,
    center_y: float,
    tolerance: float,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[tuple[dict[str, Any], dict[str, Any]]]]:
    vertical: list[tuple[dict[str, Any], dict[str, Any]]] = []
    horizontal: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for left in circles:
        for right in circles:
            if left is right or abs(left["radius"] - right["radius"]) > tolerance:
                continue
            if (
                left["x"] < center_x - tolerance
                and right["x"] > center_x + tolerance
                and abs((left["x"] + right["x"]) - 2 * center_x) <= tolerance
                and abs(left["y"] - right["y"]) <= tolerance
            ):
                pair = (left, right)
                if pair not in vertical:
                    vertical.append(pair)
            if (
                left["y"] < center_y - tolerance
                and right["y"] > center_y + tolerance
                and abs((left["y"] + right["y"]) - 2 * center_y) <= tolerance
                and abs(left["x"] - right["x"]) <= tolerance
            ):
                pair = (left, right)
                if pair not in horizontal:
                    horizontal.append(pair)
    return vertical, horizontal


async def run_ezdxf_auto_dimension(
    backend: Any,
    options: AutoDimensionOptions,
) -> CommandResult:
    """Headless automatic dimensioning used by the ezdxf backend and tests."""

    document = backend._doc  # noqa: SLF001 - same package backend extension
    modelspace = backend._msp  # noqa: SLF001
    if document is None or modelspace is None:
        return CommandResult(ok=False, error="No document open")

    if options.dimension_layer not in document.layers:
        document.layers.add(options.dimension_layer, color=2)

    if options.clear_existing:
        for entity in list(modelspace):
            if entity.dxf.get("layer", "0") == options.dimension_layer:
                modelspace.delete_entity(entity)

    geometry: list[Any] = []
    points: list[tuple[float, float]] = []
    circles: list[dict[str, Any]] = []
    arcs: list[dict[str, Any]] = []
    unsupported = 0
    allowed_layers = set(options.source_layers)

    min_x = math.inf
    min_y = math.inf
    max_x = -math.inf
    max_y = -math.inf

    for entity in list(modelspace):
        entity_type = entity.dxftype()
        layer = entity.dxf.get("layer", "0")
        if layer in {options.dimension_layer, "DEFPOINTS"}:
            continue
        if allowed_layers and layer not in allowed_layers:
            continue
        if entity_type in {"DIMENSION", "TEXT", "MTEXT", "LEADER", "MLEADER", "HATCH"}:
            continue

        entity_points = _entity_points(entity)
        if entity_points:
            geometry.append(entity)
            points.extend(entity_points)
            for x, y in entity_points:
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)
            continue

        if entity_type in {"CIRCLE", "ARC"}:
            center_x = float(entity.dxf.center.x)
            center_y = float(entity.dxf.center.y)
            radius = float(entity.dxf.radius)
            feature = {
                "entity": entity,
                "x": center_x,
                "y": center_y,
                "radius": radius,
            }
            geometry.append(entity)
            points.append((center_x, center_y))
            min_x = min(min_x, center_x - radius)
            min_y = min(min_y, center_y - radius)
            max_x = max(max_x, center_x + radius)
            max_y = max(max_y, center_y + radius)
            if entity_type == "CIRCLE":
                circles.append(feature)
            else:
                arcs.append(feature)
            continue

        unsupported += 1

    if not geometry or not math.isfinite(min_x):
        return CommandResult(
            ok=False,
            error="No supported Model Space geometry found (LINE, POLYLINE, CIRCLE, ARC).",
        )

    width = max_x - min_x
    height = max_y - min_y
    scale = max(width, height, 1.0)
    spacing = options.spacing or max(scale * 0.045, 5.0)
    tolerance = max(scale * 1e-5, 1e-6)
    min_segment = spacing * 0.65
    mode_cap = {"minimal": 2, "balanced": 12, "detailed": 24}[options.mode]

    existing_dimensions = sum(1 for entity in modelspace if entity.dxftype() == "DIMENSION")
    first_lane = spacing * (1.5 + min(existing_dimensions, 4) * 0.35)
    created = 0
    overall_count = 0
    feature_count = 0
    hole_count = 0
    arc_count = 0
    center_count = 0
    symmetry_count = 0
    skipped_short = 0

    if options.include_overall:
        if _add_linear_dimension(
            modelspace,
            p1=(min_x, min_y),
            p2=(max_x, min_y),
            base=(min_x, min_y - first_lane),
            angle=0,
            layer=options.dimension_layer,
        ):
            created += 1
            overall_count += 1
        if _add_linear_dimension(
            modelspace,
            p1=(min_x, min_y),
            p2=(min_x, max_y),
            base=(min_x - first_lane, min_y),
            angle=90,
            layer=options.dimension_layer,
        ):
            created += 1
            overall_count += 1

    if options.include_features and options.mode != "minimal":
        x_values = _thin_coordinates(
            _unique_sorted((point[0] for point in points), tolerance),
            mode_cap,
        )
        y_values = _thin_coordinates(
            _unique_sorted((point[1] for point in points), tolerance),
            mode_cap,
        )
        chain_lane = first_lane + spacing
        for left, right in zip(x_values, x_values[1:]):
            if right - left < min_segment:
                skipped_short += 1
                continue
            if _add_linear_dimension(
                modelspace,
                p1=(left, min_y),
                p2=(right, min_y),
                base=(left, min_y - chain_lane),
                angle=0,
                layer=options.dimension_layer,
            ):
                created += 1
                feature_count += 1
        for bottom, top in zip(y_values, y_values[1:]):
            if top - bottom < min_segment:
                skipped_short += 1
                continue
            if _add_linear_dimension(
                modelspace,
                p1=(min_x, bottom),
                p2=(min_x, top),
                base=(min_x - chain_lane, bottom),
                angle=90,
                layer=options.dimension_layer,
            ):
                created += 1
                feature_count += 1

    feature_angles = (45.0, 135.0, 225.0, 315.0)
    if options.include_holes:
        for index, circle in enumerate(circles):
            angle = math.radians(feature_angles[index % len(feature_angles)])
            lane = 1 + index // len(feature_angles)
            distance = circle["radius"] + spacing * lane
            point = (
                circle["x"] + distance * math.cos(angle),
                circle["y"] + distance * math.sin(angle),
            )
            if _add_radius_dimension(
                modelspace,
                center=(circle["x"], circle["y"]),
                point=point,
                layer=options.dimension_layer,
                diameter=True,
            ):
                created += 1
                hole_count += 1
            if options.include_centers:
                mark = min(max(circle["radius"] * 0.22, spacing * 0.12), spacing * 0.4)
                modelspace.add_line(
                    (circle["x"] - mark, circle["y"]),
                    (circle["x"] + mark, circle["y"]),
                    dxfattribs={"layer": options.dimension_layer},
                )
                modelspace.add_line(
                    (circle["x"], circle["y"] - mark),
                    (circle["x"], circle["y"] + mark),
                    dxfattribs={"layer": options.dimension_layer},
                )
                center_count += 1

    if options.include_arcs:
        for index, arc in enumerate(arcs):
            angle = math.radians(feature_angles[(index + 1) % len(feature_angles)])
            distance = arc["radius"] + spacing * (1 + index // len(feature_angles))
            point = (
                arc["x"] + distance * math.cos(angle),
                arc["y"] + distance * math.sin(angle),
            )
            if _add_radius_dimension(
                modelspace,
                center=(arc["x"], arc["y"]),
                point=point,
                layer=options.dimension_layer,
                diameter=False,
            ):
                created += 1
                arc_count += 1

    vertical_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    horizontal_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if options.detect_symmetry and len(circles) >= 2:
        center_x = (min_x + max_x) / 2
        center_y = (min_y + max_y) / 2
        symmetry_tolerance = max(tolerance * 10, spacing * 0.08)
        vertical_pairs, horizontal_pairs = _find_symmetric_pairs(
            circles,
            center_x,
            center_y,
            symmetry_tolerance,
        )
        for index, (left, right) in enumerate(vertical_pairs[:8]):
            base_y = max_y + first_lane + spacing * (index + 1)
            if _add_linear_dimension(
                modelspace,
                p1=(left["x"], left["y"]),
                p2=(right["x"], right["y"]),
                base=(left["x"], base_y),
                angle=0,
                layer=options.dimension_layer,
            ):
                created += 1
                symmetry_count += 1
        for index, (bottom, top) in enumerate(horizontal_pairs[:8]):
            base_x = max_x + first_lane + spacing * (index + 1)
            if _add_linear_dimension(
                modelspace,
                p1=(bottom["x"], bottom["y"]),
                p2=(top["x"], top["y"]),
                base=(base_x, bottom["y"]),
                angle=90,
                layer=options.dimension_layer,
            ):
                created += 1
                symmetry_count += 1

    return CommandResult(
        ok=True,
        payload={
            "ok": True,
            "backend": "ezdxf",
            "mode": options.mode,
            "geometry_count": len(geometry),
            "unsupported_entities": unsupported,
            "circle_count": len(circles),
            "arc_count": len(arcs),
            "dimensions_created": created,
            "overall_dimensions": overall_count,
            "feature_dimensions": feature_count,
            "hole_dimensions": hole_count,
            "arc_dimensions": arc_count,
            "center_marks": center_count,
            "symmetry_dimensions": symmetry_count,
            "vertical_symmetry_pairs": len(vertical_pairs),
            "horizontal_symmetry_pairs": len(horizontal_pairs),
            "skipped_short_segments": skipped_short,
            "dimension_layer": options.dimension_layer,
            "spacing": spacing,
            "extents": {
                "min": [min_x, min_y],
                "max": [max_x, max_y],
            },
            "preview": "attached when include_screenshot=true",
        },
    )
