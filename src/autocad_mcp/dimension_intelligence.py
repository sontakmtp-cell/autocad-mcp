"""Mechanical feature recognition and deterministic dimension QA helpers.

The functions in this module deliberately do not register MCP tools.  They are
small, backend-independent building blocks that work with an ezdxf Modelspace
or any iterable exposing the same entity attributes.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence


Point = tuple[float, float]


class _RecordPoint:
    def __init__(self, value: Sequence[float]):
        self.x = float(value[0])
        self.y = float(value[1])


class _RecordDXF:
    """Minimal DXF namespace for JSON entity records."""

    _POINT_FIELDS = {
        "start",
        "end",
        "center",
        "defpoint",
        "defpoint2",
        "defpoint3",
        "defpoint4",
        "defpoint5",
        "text_midpoint",
    }

    def __init__(self, record: dict[str, Any]):
        self._record = record

    def get(self, name: str, default: Any = None) -> Any:
        value = self._record.get(name, default)
        if value is None and name in {"start", "end"}:
            points = self._record.get("points", [])
            if points:
                value = points[0 if name == "start" else -1]
        if name in self._POINT_FIELDS and isinstance(value, (list, tuple)):
            return _RecordPoint(value)
        return value

    def __getattr__(self, name: str) -> Any:
        value = self.get(name)
        if value is None and name not in self._record:
            raise AttributeError(name)
        return value


class _RecordEntity:
    """Read-only entity facade for File IPC/plan JSON records."""

    def __init__(self, record: dict[str, Any]):
        self.record = record
        self.dxf = _RecordDXF(record)
        self.closed = bool(record.get("closed", False))

    def dxftype(self) -> str:
        return str(self.record.get("type", self.record.get("entity_type", ""))).upper()

    def get_points(self, _format: str) -> list[Point]:
        return [tuple(map(float, point[:2])) for point in self.record.get("points", [])]

    def get_measurement(self) -> float:
        return float(self.record["measurement"])


def _coerce_entities(entities: Iterable[Any]) -> list[Any]:
    return [_RecordEntity(entity) if isinstance(entity, dict) else entity for entity in entities]


@dataclass(frozen=True)
class MechanicalFeature:
    """A manufacturing feature inferred from exact drawing geometry."""

    feature_id: str
    kind: str
    entity_handles: tuple[str, ...]
    geometry: dict[str, Any]
    notation: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MechanicalFeatureReport:
    features: tuple[MechanicalFeature, ...]
    source_entity_count: int
    tolerance: float

    def by_kind(self, kind: str) -> tuple[MechanicalFeature, ...]:
        return tuple(feature for feature in self.features if feature.kind == kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "features": [feature.to_dict() for feature in self.features],
            "source_entity_count": self.source_entity_count,
            "tolerance": self.tolerance,
            "counts": {
                kind: sum(feature.kind == kind for feature in self.features)
                for kind in sorted({feature.kind for feature in self.features})
            },
        }


@dataclass(frozen=True)
class DimensionIssue:
    issue_id: str
    code: str
    severity: str
    message: str
    dimension_handles: tuple[str, ...] = ()
    repairable: bool = False
    suggested_action: str = "review"
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DimensionAuditReport:
    issues: tuple[DimensionIssue, ...]
    dimension_count: int
    geometry_count: int
    expected_layer: str
    expected_style: str | None
    extents: tuple[Point, Point] | None

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dimension_count": self.dimension_count,
            "geometry_count": self.geometry_count,
            "expected_layer": self.expected_layer,
            "expected_style": self.expected_style,
            "extents": None
            if self.extents is None
            else {"min": list(self.extents[0]), "max": list(self.extents[1])},
            "issues": [issue.to_dict() for issue in self.issues],
            "counts": {
                level: sum(issue.severity == level for issue in self.issues)
                for level in ("error", "warning", "info")
            },
        }


@dataclass(frozen=True)
class DimensionRepairResult:
    applied: bool
    actions: tuple[dict[str, Any], ...]
    unresolved_issue_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _xy(value: Any) -> Point:
    return (float(value.x), float(value.y))


def _handle(entity: Any) -> str:
    return str(entity.dxf.get("handle", "") or "")


def _close(left: Point, right: Point, tolerance: float) -> bool:
    return math.dist(left, right) <= tolerance


def _round_point(point: Point, tolerance: float) -> tuple[int, int]:
    return (round(point[0] / tolerance), round(point[1] / tolerance))


def _arc_sweep(entity: Any) -> float:
    return (float(entity.dxf.end_angle) - float(entity.dxf.start_angle)) % 360.0


def _arc_endpoints(entity: Any) -> tuple[Point, Point]:
    center = _xy(entity.dxf.center)
    radius = float(entity.dxf.radius)
    result: list[Point] = []
    for angle in (float(entity.dxf.start_angle), float(entity.dxf.end_angle)):
        radians = math.radians(angle)
        result.append(
            (center[0] + radius * math.cos(radians), center[1] + radius * math.sin(radians))
        )
    return result[0], result[1]


def _polyline_points(entity: Any) -> list[Point]:
    if entity.dxftype() == "LWPOLYLINE":
        return [(float(x), float(y)) for x, y in entity.get_points("xy")]
    if entity.dxftype() == "POLYLINE":
        if isinstance(entity, _RecordEntity):
            return entity.get_points("xy")
        return [_xy(vertex.dxf.location) for vertex in entity.vertices]
    return []


def _line_segments(entity: Any) -> list[tuple[Point, Point]]:
    entity_type = entity.dxftype()
    if entity_type == "LINE":
        return [(_xy(entity.dxf.start), _xy(entity.dxf.end))]
    points = _polyline_points(entity)
    if len(points) < 2:
        return []
    segments = list(zip(points, points[1:]))
    if bool(entity.closed):
        segments.append((points[-1], points[0]))
    return segments


def _entity_extents(entity: Any) -> tuple[Point, Point] | None:
    entity_type = entity.dxftype()
    points = [point for segment in _line_segments(entity) for point in segment]
    if points:
        return (
            (min(point[0] for point in points), min(point[1] for point in points)),
            (max(point[0] for point in points), max(point[1] for point in points)),
        )
    if entity_type in {"CIRCLE", "ARC"}:
        center = _xy(entity.dxf.center)
        radius = float(entity.dxf.radius)
        # A full-radius box is conservative for partial arcs and suitable for QA.
        return (
            (center[0] - radius, center[1] - radius),
            (center[0] + radius, center[1] + radius),
        )
    return None


def _drawing_extents(entities: Sequence[Any]) -> tuple[Point, Point] | None:
    boxes = [box for entity in entities if (box := _entity_extents(entity))]
    if not boxes:
        return None
    return (
        (min(box[0][0] for box in boxes), min(box[0][1] for box in boxes)),
        (max(box[1][0] for box in boxes), max(box[1][1] for box in boxes)),
    )


def _default_tolerance(entities: Sequence[Any]) -> float:
    extents = _drawing_extents(entities)
    if extents is None:
        return 1e-6
    width = extents[1][0] - extents[0][0]
    height = extents[1][1] - extents[0][1]
    return max(width, height, 1.0) * 1e-5


def _line_connects(
    segments: Sequence[tuple[Point, Point]],
    left: Point,
    right: Point,
    tolerance: float,
) -> bool:
    return any(
        (_close(start, left, tolerance) and _close(end, right, tolerance))
        or (_close(start, right, tolerance) and _close(end, left, tolerance))
        for start, end in segments
    )


def recognize_mechanical_features(
    entities: Iterable[Any],
    *,
    tolerance: float | None = None,
) -> MechanicalFeatureReport:
    """Recognize deterministic 2D mechanical features.

    Supported patterns are repeated holes, concentric circular geometry,
    connected obround slots, 45-degree polyline chamfers, repeated fillet arcs,
    and equal-hole symmetry about the source geometry bounding box.
    """

    source = [
        entity
        for entity in _coerce_entities(entities)
        if entity.dxftype() not in {"DIMENSION", "TEXT", "MTEXT", "LEADER", "MLEADER"}
    ]
    tol = float(tolerance) if tolerance is not None else _default_tolerance(source)
    if tol <= 0:
        raise ValueError("tolerance must be greater than zero")

    circles = [entity for entity in source if entity.dxftype() == "CIRCLE"]
    arcs = [entity for entity in source if entity.dxftype() == "ARC"]
    segments = [segment for entity in source for segment in _line_segments(entity)]
    features: list[MechanicalFeature] = []

    def add(
        kind: str,
        members: Sequence[Any],
        geometry: dict[str, Any],
        notation: str,
        confidence: float,
    ) -> None:
        features.append(
            MechanicalFeature(
                feature_id=f"F{len(features) + 1}",
                kind=kind,
                entity_handles=tuple(_handle(member) for member in members),
                geometry=geometry,
                notation=notation,
                confidence=confidence,
            )
        )

    radius_groups: dict[int, list[Any]] = {}
    for circle in circles:
        radius_groups.setdefault(round(float(circle.dxf.radius) / tol), []).append(circle)
    for group in radius_groups.values():
        if len(group) < 2:
            continue
        radius = sum(float(item.dxf.radius) for item in group) / len(group)
        centers = [_xy(item.dxf.center) for item in group]
        add(
            "repeated_hole_pattern",
            group,
            {"quantity": len(group), "diameter": radius * 2, "centers": centers},
            f"{len(group)}x ⌀{radius * 2:g}",
            1.0,
        )

    center_groups: dict[tuple[int, int], list[Any]] = {}
    for entity in [*circles, *arcs]:
        center_groups.setdefault(_round_point(_xy(entity.dxf.center), tol), []).append(entity)
    for group in center_groups.values():
        unique_radii = sorted({round(float(item.dxf.radius) / tol) for item in group})
        if len(group) < 2 or len(unique_radii) < 2:
            continue
        add(
            "concentric_group",
            group,
            {
                "center": _xy(group[0].dxf.center),
                "radii": sorted(float(item.dxf.radius) for item in group),
            },
            "Concentric ⌀" + "/".join(f"{float(item.dxf.radius) * 2:g}" for item in group),
            1.0,
        )

    used_slot_arcs: set[str] = set()
    for index, left in enumerate(arcs):
        if abs(_arc_sweep(left) - 180.0) > 2.0:
            continue
        for right in arcs[index + 1 :]:
            if abs(_arc_sweep(right) - 180.0) > 2.0:
                continue
            radius = (float(left.dxf.radius) + float(right.dxf.radius)) / 2
            if abs(float(left.dxf.radius) - float(right.dxf.radius)) > tol:
                continue
            center_distance = math.dist(_xy(left.dxf.center), _xy(right.dxf.center))
            if center_distance <= tol:
                continue
            left_ends = _arc_endpoints(left)
            right_ends = _arc_endpoints(right)
            connected = (
                _line_connects(segments, left_ends[0], right_ends[0], tol * 2)
                and _line_connects(segments, left_ends[1], right_ends[1], tol * 2)
            ) or (
                _line_connects(segments, left_ends[0], right_ends[1], tol * 2)
                and _line_connects(segments, left_ends[1], right_ends[0], tol * 2)
            )
            if not connected:
                continue
            add(
                "slot",
                (left, right),
                {
                    "centers": [_xy(left.dxf.center), _xy(right.dxf.center)],
                    "width": radius * 2,
                    "length": center_distance + radius * 2,
                    "center_distance": center_distance,
                },
                f"SLOT {center_distance + radius * 2:g} x {radius * 2:g}",
                1.0,
            )
            used_slot_arcs.update({_handle(left), _handle(right)})
            break

    fillet_groups: dict[int, list[Any]] = {}
    for arc in arcs:
        if _handle(arc) in used_slot_arcs:
            continue
        fillet_groups.setdefault(round(float(arc.dxf.radius) / tol), []).append(arc)
    for group in fillet_groups.values():
        if len(group) < 2:
            continue
        radius = sum(float(item.dxf.radius) for item in group) / len(group)
        add(
            "repeated_fillet",
            group,
            {"quantity": len(group), "radius": radius},
            f"{len(group)}x R{radius:g}",
            0.9,
        )

    for polyline in [entity for entity in source if entity.dxftype() in {"LWPOLYLINE", "POLYLINE"}]:
        poly_segments = _line_segments(polyline)
        if len(poly_segments) < 3:
            continue
        for index, (start, end) in enumerate(poly_segments):
            dx, dy = end[0] - start[0], end[1] - start[1]
            length = math.hypot(dx, dy)
            if length <= tol or min(abs(dx), abs(dy)) <= tol:
                continue
            angle = abs(math.degrees(math.atan2(dy, dx))) % 90.0
            angle = min(angle, 90.0 - angle)
            if abs(angle - 45.0) > 2.0:
                continue
            previous = poly_segments[index - 1]
            following = poly_segments[(index + 1) % len(poly_segments)]
            previous_axis = abs(previous[1][0] - previous[0][0]) <= tol or abs(
                previous[1][1] - previous[0][1]
            ) <= tol
            following_axis = abs(following[1][0] - following[0][0]) <= tol or abs(
                following[1][1] - following[0][1]
            ) <= tol
            if previous_axis and following_axis:
                add(
                    "chamfer",
                    (polyline,),
                    {"start": start, "end": end, "length": length, "angle": 45.0},
                    f"{min(abs(dx), abs(dy)):g} x 45°",
                    0.95,
                )

    extents = _drawing_extents(source)
    if extents and len(circles) >= 2:
        center_x = (extents[0][0] + extents[1][0]) / 2
        center_y = (extents[0][1] + extents[1][1]) / 2
        pairs: list[tuple[Any, Any, str]] = []
        used: set[tuple[str, str, str]] = set()
        for index, left in enumerate(circles):
            for right in circles[index + 1 :]:
                if abs(float(left.dxf.radius) - float(right.dxf.radius)) > tol:
                    continue
                left_center, right_center = _xy(left.dxf.center), _xy(right.dxf.center)
                axis = "vertical" if (
                    abs(left_center[1] - right_center[1]) <= tol
                    and abs(left_center[0] + right_center[0] - 2 * center_x) <= tol * 2
                ) else "horizontal" if (
                    abs(left_center[0] - right_center[0]) <= tol
                    and abs(left_center[1] + right_center[1] - 2 * center_y) <= tol * 2
                ) else ""
                key = tuple(sorted((_handle(left), _handle(right)))) + (axis,)
                if axis and key not in used:
                    used.add(key)
                    pairs.append((left, right, axis))
        if pairs:
            members = [member for pair in pairs for member in pair[:2]]
            add(
                "symmetric_hole_pattern",
                members,
                {
                    "axes": sorted({pair[2] for pair in pairs}),
                    "center": (center_x, center_y),
                    "pairs": [
                        [_xy(pair[0].dxf.center), _xy(pair[1].dxf.center)] for pair in pairs
                    ],
                },
                "Symmetric hole pattern",
                0.98,
            )

    return MechanicalFeatureReport(tuple(features), len(source), tol)


def _dimension_signature(entity: Any, tolerance: float) -> tuple[Any, ...]:
    dimtype = int(entity.dxf.get("dimtype", 0)) & 15
    points: list[tuple[int, int]] = []
    for name in ("defpoint", "defpoint2", "defpoint3", "defpoint4", "defpoint5"):
        value = entity.dxf.get(name)
        if value is not None:
            points.append(_round_point(_xy(value), tolerance))
    if len(points) >= 3:
        references = sorted(points[1:3])
        points[1:3] = references
    return (dimtype, tuple(points), round(float(entity.dxf.get("angle", 0.0)), 4))


def _dimension_measurement(entity: Any) -> float | None:
    try:
        measurement = float(entity.get_measurement())
        return measurement if math.isfinite(measurement) else None
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None


def _dimension_text_box(entity: Any, text_height: float) -> tuple[Point, Point] | None:
    midpoint = entity.dxf.get("text_midpoint")
    if midpoint is None:
        return None
    center = _xy(midpoint)
    measurement = _dimension_measurement(entity)
    override = str(entity.dxf.get("text", "<>"))
    label = override if override not in {"", "<>"} else f"{measurement or 0:g}"
    half_width = max(len(label), 1) * text_height * 0.35
    half_height = text_height * 0.65
    return (
        (center[0] - half_width, center[1] - half_height),
        (center[0] + half_width, center[1] + half_height),
    )


def _boxes_overlap(left: tuple[Point, Point], right: tuple[Point, Point]) -> bool:
    return not (
        left[1][0] < right[0][0]
        or right[1][0] < left[0][0]
        or left[1][1] < right[0][1]
        or right[1][1] < left[0][1]
    )


def _orientation(entity: Any, tolerance: float) -> str | None:
    p1 = entity.dxf.get("defpoint2")
    p2 = entity.dxf.get("defpoint3")
    if p1 is None or p2 is None:
        return None
    first, second = _xy(p1), _xy(p2)
    if abs(first[1] - second[1]) <= tolerance:
        return "horizontal"
    if abs(first[0] - second[0]) <= tolerance:
        return "vertical"
    return "aligned"


def _dimension_line(entity: Any) -> tuple[Point, Point] | None:
    base = entity.dxf.get("defpoint")
    p1 = entity.dxf.get("defpoint2")
    p2 = entity.dxf.get("defpoint3")
    if base is None or p1 is None or p2 is None:
        return None
    base_point, first, second = _xy(base), _xy(p1), _xy(p2)
    angle = math.radians(float(entity.dxf.get("angle", 0.0)))
    direction = (math.cos(angle), math.sin(angle))

    def project(point: Point) -> Point:
        distance = (point[0] - base_point[0]) * direction[0] + (
            point[1] - base_point[1]
        ) * direction[1]
        return (
            base_point[0] + distance * direction[0],
            base_point[1] + distance * direction[1],
        )

    return project(first), project(second)


def _cross(left: Point, right: Point, point: Point) -> float:
    return (right[0] - left[0]) * (point[1] - left[1]) - (
        right[1] - left[1]
    ) * (point[0] - left[0])


def _proper_intersection(
    first: tuple[Point, Point],
    second: tuple[Point, Point],
    tolerance: float,
) -> bool:
    a, b = first
    c, d = second
    if any(_close(left, right, tolerance) for left in first for right in second):
        return False
    ab_c, ab_d = _cross(a, b, c), _cross(a, b, d)
    cd_a, cd_b = _cross(c, d, a), _cross(c, d, b)
    return ab_c * ab_d < -(tolerance**2) and cd_a * cd_b < -(tolerance**2)


def _point_on_segment(point: Point, segment: tuple[Point, Point], tolerance: float) -> bool:
    start, end = segment
    length = math.dist(start, end)
    if length <= tolerance:
        return _close(point, start, tolerance)
    cross_distance = abs(_cross(start, end, point)) / length
    dot = (point[0] - start[0]) * (end[0] - start[0]) + (
        point[1] - start[1]
    ) * (end[1] - start[1])
    return cross_distance <= tolerance and -tolerance <= dot <= length * length + tolerance


def _point_on_geometry(point: Point, geometry: Sequence[Any], tolerance: float) -> bool:
    for entity in geometry:
        if any(_point_on_segment(point, segment, tolerance) for segment in _line_segments(entity)):
            return True
        if entity.dxftype() in {"CIRCLE", "ARC"}:
            center = _xy(entity.dxf.center)
            radius = float(entity.dxf.radius)
            distance = math.dist(point, center)
            if distance <= tolerance or abs(distance - radius) <= tolerance:
                return True
    return False


def _dimension_interval(entity: Any, tolerance: float) -> tuple[str, float, float] | None:
    orientation = _orientation(entity, tolerance)
    p1 = entity.dxf.get("defpoint2")
    p2 = entity.dxf.get("defpoint3")
    if orientation not in {"horizontal", "vertical"} or p1 is None or p2 is None:
        return None
    first, second = _xy(p1), _xy(p2)
    values = (first[0], second[0]) if orientation == "horizontal" else (first[1], second[1])
    return orientation, min(values), max(values)


def _displayed_measurement(entity: Any) -> float | None:
    override = str(entity.dxf.get("text", "<>"))
    if override not in {"", "<>"}:
        try:
            return float(override.replace(",", "."))
        except ValueError:
            pass
    return _dimension_measurement(entity)


def _find_chain_for_overall(
    overall: Any,
    candidates: Sequence[Any],
    tolerance: float,
) -> list[Any]:
    interval = _dimension_interval(overall, tolerance)
    if interval is None:
        return []
    orientation, start, end = interval
    spans = []
    for candidate in candidates:
        candidate_interval = _dimension_interval(candidate, tolerance)
        if candidate is overall or candidate_interval is None:
            continue
        candidate_orientation, left, right = candidate_interval
        if (
            candidate_orientation == orientation
            and left >= start - tolerance
            and right <= end + tolerance
            and right - left < end - start - tolerance
        ):
            spans.append((left, right, candidate))
    chain: list[Any] = []
    cursor = start
    while cursor < end - tolerance:
        matches = [item for item in spans if abs(item[0] - cursor) <= tolerance]
        if not matches:
            return []
        selected = max(matches, key=lambda item: item[1])
        if selected[1] <= cursor + tolerance:
            return []
        chain.append(selected[2])
        cursor = selected[1]
    return chain if len(chain) >= 2 and abs(cursor - end) <= tolerance else []


def _collinear_overlap(
    first: tuple[Point, Point],
    second: tuple[Point, Point],
    tolerance: float,
) -> bool:
    if abs(_cross(first[0], first[1], second[0])) > tolerance * max(
        math.dist(first[0], first[1]), 1.0
    ):
        return False
    if abs(_cross(first[0], first[1], second[1])) > tolerance * max(
        math.dist(first[0], first[1]), 1.0
    ):
        return False
    dx = abs(first[1][0] - first[0][0])
    dy = abs(first[1][1] - first[0][1])
    axis = 0 if dx >= dy else 1
    left = sorted((first[0][axis], first[1][axis]))
    right = sorted((second[0][axis], second[1][axis]))
    return min(left[1], right[1]) - max(left[0], right[0]) > tolerance


def audit_dimensions(
    entities: Iterable[Any],
    *,
    expected_layer: str = "MCP-DIM",
    expected_style: str | None = None,
    tolerance: float | None = None,
    text_height: float = 2.5,
) -> DimensionAuditReport:
    """Audit dimensions without mutating the drawing."""

    source = _coerce_entities(entities)
    dimensions = [entity for entity in source if entity.dxftype() == "DIMENSION"]
    geometry = [
        entity
        for entity in source
        if entity.dxftype() in {"LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC"}
        and entity.dxf.get("layer", "0") != expected_layer
    ]
    tol = float(tolerance) if tolerance is not None else _default_tolerance(geometry)
    if tol <= 0:
        raise ValueError("tolerance must be greater than zero")
    if text_height <= 0:
        raise ValueError("text_height must be greater than zero")
    extents = _drawing_extents(geometry)
    pending: list[dict[str, Any]] = []

    signatures: dict[tuple[Any, ...], list[Any]] = {}
    for dimension in dimensions:
        signatures.setdefault(_dimension_signature(dimension, tol), []).append(dimension)
        handle = _handle(dimension)
        layer = str(dimension.dxf.get("layer", "0"))
        if layer != expected_layer:
            pending.append(
                dict(
                    code="wrong_layer",
                    severity="warning",
                    message=f"Dimension {handle} is on layer {layer!r}, expected {expected_layer!r}.",
                    dimension_handles=(handle,),
                    repairable=True,
                    suggested_action="set_layer",
                    data={"actual": layer, "expected": expected_layer},
                )
            )
        style = str(dimension.dxf.get("dimstyle", "Standard"))
        if expected_style and style != expected_style:
            pending.append(
                dict(
                    code="wrong_style",
                    severity="warning",
                    message=f"Dimension {handle} uses style {style!r}, expected {expected_style!r}.",
                    dimension_handles=(handle,),
                    repairable=True,
                    suggested_action="set_style",
                    data={"actual": style, "expected": expected_style},
                )
            )

    for duplicates in signatures.values():
        if len(duplicates) > 1:
            handles = tuple(_handle(item) for item in duplicates)
            pending.append(
                dict(
                    code="duplicate_dimension",
                    severity="error",
                    message=f"Dimensions {', '.join(handles)} measure the same references.",
                    dimension_handles=handles,
                    repairable=True,
                    suggested_action="delete_duplicates",
                    data={"keep": handles[0], "delete": list(handles[1:])},
                )
            )

    text_boxes = [(dimension, _dimension_text_box(dimension, text_height)) for dimension in dimensions]
    for index, (left, left_box) in enumerate(text_boxes):
        if left_box is None:
            continue
        for right, right_box in text_boxes[index + 1 :]:
            if right_box is not None and _boxes_overlap(left_box, right_box):
                handles = (_handle(left), _handle(right))
                pending.append(
                    dict(
                        code="text_overlap",
                        severity="error",
                        message=f"Dimension text overlaps for {handles[0]} and {handles[1]}.",
                        dimension_handles=handles,
                        repairable=True,
                        suggested_action="move_to_next_lane",
                        data={},
                    )
                )

    geometry_segments = [segment for entity in geometry for segment in _line_segments(entity)]
    for dimension in dimensions:
        dim_line = _dimension_line(dimension)
        if dim_line and any(
            _proper_intersection(dim_line, segment, tol) for segment in geometry_segments
        ):
            handle = _handle(dimension)
            pending.append(
                dict(
                    code="dimension_crosses_geometry",
                    severity="error",
                    message=f"Dimension line {handle} crosses source geometry.",
                    dimension_handles=(handle,),
                    repairable=True,
                    suggested_action="move_outside_geometry",
                    data={},
                )
            )

    dimension_lines = [(dimension, _dimension_line(dimension)) for dimension in dimensions]
    for index, (left, left_line) in enumerate(dimension_lines):
        if left_line is None:
            continue
        for right, right_line in dimension_lines[index + 1 :]:
            if right_line is None or not _collinear_overlap(left_line, right_line, tol):
                continue
            handles = (_handle(left), _handle(right))
            # Exact duplicates already have a clearer, safer delete action.
            if _dimension_signature(left, tol) == _dimension_signature(right, tol):
                continue
            pending.append(
                dict(
                    code="dimension_line_overlap",
                    severity="warning",
                    message=f"Dimension lines overlap for {handles[0]} and {handles[1]}.",
                    dimension_handles=handles,
                    repairable=True,
                    suggested_action="move_to_next_lane",
                    data={},
                )
            )

    for dimension in dimensions:
        interval = _dimension_interval(dimension, tol)
        if interval is None:
            continue
        references = [dimension.dxf.get("defpoint2"), dimension.dxf.get("defpoint3")]
        detached = [
            _xy(value)
            for value in references
            if value is not None and not _point_on_geometry(_xy(value), geometry, tol * 5)
        ]
        if detached:
            handle = _handle(dimension)
            pending.append(
                dict(
                    code="detached_geometry_reference",
                    severity="error",
                    message=f"Dimension {handle} has a reference point detached from source geometry.",
                    dimension_handles=(handle,),
                    repairable=False,
                    suggested_action="reattach_or_recreate_dimension",
                    data={"detached_points": detached},
                )
            )

    for overall in dimensions:
        chain = _find_chain_for_overall(overall, dimensions, tol * 5)
        if not chain:
            continue
        overall_value = _displayed_measurement(overall)
        chain_values = [_displayed_measurement(item) for item in chain]
        if overall_value is None or any(value is None for value in chain_values):
            continue
        chain_total = sum(value for value in chain_values if value is not None)
        display_tolerance = max(tol * 5, 1e-6)
        if abs(chain_total - overall_value) > display_tolerance:
            handles = (_handle(overall), *(_handle(item) for item in chain))
            pending.append(
                dict(
                    code="chain_accumulation_error",
                    severity="error",
                    message=(
                        f"Displayed chain total {chain_total:g} does not match overall "
                        f"dimension {overall_value:g}."
                    ),
                    dimension_handles=handles,
                    repairable=False,
                    suggested_action="use_baseline_or_ordinate_dimensions",
                    data={"overall": overall_value, "chain_total": chain_total},
                )
            )

    if extents:
        width = extents[1][0] - extents[0][0]
        height = extents[1][1] - extents[0][1]
        horizontal = [
            _dimension_measurement(item)
            for item in dimensions
            if _orientation(item, tol) == "horizontal"
        ]
        vertical = [
            _dimension_measurement(item)
            for item in dimensions
            if _orientation(item, tol) == "vertical"
        ]
        missing = []
        if width > tol and not any(value is not None and abs(value - width) <= tol * 5 for value in horizontal):
            missing.append("width")
        if height > tol and not any(value is not None and abs(value - height) <= tol * 5 for value in vertical):
            missing.append("height")
        if missing:
            pending.append(
                dict(
                    code="missing_overall_dimension",
                    severity="warning",
                    message=f"Missing overall {' and '.join(missing)} dimension.",
                    dimension_handles=(),
                    repairable=False,
                    suggested_action="add_overall_dimension",
                    data={"missing": missing, "width": width, "height": height},
                )
            )

    circles = [entity for entity in geometry if entity.dxftype() == "CIRCLE"]
    unlocated: list[str] = []
    for circle in circles:
        center = _xy(circle.dxf.center)
        located_x = False
        located_y = False
        for dimension in dimensions:
            orientation = _orientation(dimension, tol)
            references = [dimension.dxf.get("defpoint2"), dimension.dxf.get("defpoint3")]
            points = [_xy(value) for value in references if value is not None]
            located_x = located_x or (
                orientation == "horizontal" and any(abs(point[0] - center[0]) <= tol for point in points)
            )
            located_y = located_y or (
                orientation == "vertical" and any(abs(point[1] - center[1]) <= tol for point in points)
            )
        if not (located_x and located_y):
            unlocated.append(_handle(circle))
    if unlocated:
        pending.append(
            dict(
                code="missing_hole_location",
                severity="warning",
                message=f"{len(unlocated)} hole(s) do not have both X and Y center locations.",
                dimension_handles=(),
                repairable=False,
                suggested_action="add_center_location_dimensions",
                data={"hole_handles": unlocated},
            )
        )

    issues = tuple(
        DimensionIssue(issue_id=f"A{index:03d}", **item)
        for index, item in enumerate(pending, start=1)
    )
    return DimensionAuditReport(
        issues=issues,
        dimension_count=len(dimensions),
        geometry_count=len(geometry),
        expected_layer=expected_layer,
        expected_style=expected_style,
        extents=extents,
    )


def repair_dimension_layout(
    modelspace: Any,
    audit: DimensionAuditReport,
    *,
    issue_ids: Iterable[str] | None = None,
    spacing: float | None = None,
    apply: bool = True,
) -> DimensionRepairResult:
    """Apply only deterministic repairs from an audit report.

    Deleting duplicates, fixing layer/style and moving an existing dimension to
    another lane are deterministic.  Missing dimensions are intentionally left
    unresolved because choosing design intent belongs to the planning layer.
    """

    raw_entities = list(modelspace)
    record_input = any(isinstance(entity, dict) for entity in raw_entities)
    if record_input and apply:
        raise ValueError("JSON entity records support repair planning only; use apply=False")
    selected = set(issue_ids) if issue_ids is not None else None
    issues = [issue for issue in audit.issues if selected is None or issue.issue_id in selected]
    entities = {
        _handle(entity): entity
        for entity in _coerce_entities(raw_entities)
        if entity.dxftype() == "DIMENSION" and _handle(entity)
    }
    if spacing is None:
        if audit.extents:
            width = audit.extents[1][0] - audit.extents[0][0]
            height = audit.extents[1][1] - audit.extents[0][1]
            lane_spacing = max(width, height, 1.0) * 0.045
        else:
            lane_spacing = 5.0
    else:
        lane_spacing = float(spacing)
    if lane_spacing <= 0:
        raise ValueError("spacing must be greater than zero")

    actions: list[dict[str, Any]] = []
    resolved: set[str] = set()
    moved_handles: set[str] = set()
    deleted_handles: set[str] = set()

    for issue in issues:
        if not issue.repairable:
            continue
        if issue.code == "duplicate_dimension":
            for handle in issue.data.get("delete", []):
                entity = entities.get(str(handle))
                if entity is None:
                    continue
                actions.append({"issue_id": issue.issue_id, "action": "delete", "handle": handle})
                if apply:
                    modelspace.delete_entity(entity)
                deleted_handles.add(str(handle))
                resolved.add(issue.issue_id)
        elif issue.code == "wrong_layer":
            handle = issue.dimension_handles[0]
            entity = entities.get(handle)
            if entity is not None and handle not in deleted_handles:
                actions.append(
                    {
                        "issue_id": issue.issue_id,
                        "action": "set_layer",
                        "handle": handle,
                        "value": audit.expected_layer,
                    }
                )
                if apply:
                    entity.dxf.layer = audit.expected_layer
                    entity.render()
                resolved.add(issue.issue_id)
        elif issue.code == "wrong_style" and audit.expected_style:
            handle = issue.dimension_handles[0]
            entity = entities.get(handle)
            if entity is not None and handle not in deleted_handles:
                actions.append(
                    {
                        "issue_id": issue.issue_id,
                        "action": "set_style",
                        "handle": handle,
                        "value": audit.expected_style,
                    }
                )
                if apply:
                    entity.dxf.dimstyle = audit.expected_style
                    entity.render()
                resolved.add(issue.issue_id)
        elif issue.code in {
            "text_overlap",
            "dimension_line_overlap",
            "dimension_crosses_geometry",
        }:
            handle = issue.dimension_handles[-1]
            if handle in moved_handles or handle in deleted_handles:
                continue
            entity = entities.get(handle)
            base = entity.dxf.get("defpoint") if entity is not None else None
            midpoint = entity.dxf.get("text_midpoint") if entity is not None else None
            if entity is None or base is None:
                continue
            dimtype = int(entity.dxf.get("dimtype", 0)) & 15
            if dimtype not in {0, 1}:
                # Radius/diameter/angular defpoints encode measured geometry,
                # not a freely movable outside lane.
                continue
            orientation = _orientation(entity, audit.extents and 1e-6 or 1e-6)
            base_point = _xy(base)
            if orientation == "vertical":
                direction = -1.0
                if audit.extents and base_point[0] > (audit.extents[0][0] + audit.extents[1][0]) / 2:
                    direction = 1.0
                delta = (direction * lane_spacing, 0.0)
            else:
                direction = -1.0
                if audit.extents and base_point[1] > (audit.extents[0][1] + audit.extents[1][1]) / 2:
                    direction = 1.0
                delta = (0.0, direction * lane_spacing)
            actions.append(
                {
                    "issue_id": issue.issue_id,
                    "action": "move_to_next_lane",
                    "handle": handle,
                    "delta": delta,
                }
            )
            if apply:
                entity.dxf.defpoint = (base_point[0] + delta[0], base_point[1] + delta[1], 0.0)
                if midpoint is not None:
                    text_point = _xy(midpoint)
                    entity.dxf.text_midpoint = (
                        text_point[0] + delta[0],
                        text_point[1] + delta[1],
                        0.0,
                    )
                entity.render()
            moved_handles.add(handle)
            resolved.add(issue.issue_id)

    unresolved = tuple(issue.issue_id for issue in issues if issue.issue_id not in resolved)
    return DimensionRepairResult(apply, tuple(actions), unresolved)
