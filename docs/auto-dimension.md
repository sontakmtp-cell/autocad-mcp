# Automatic dimensioning

The server exposes one high-level MCP tool:

```text
annotation.auto_dimension
```

Unlike the individual linear/aligned/radius dimension operations, this tool
analyzes Model Space inside the AutoCAD process and creates the full layout in a
single IPC request. This removes the slow loop where an LLM has to inspect and
place every dimension separately.

## Example

```json
{
  "data": {
    "mode": "balanced",
    "include_overall": true,
    "include_features": true,
    "include_holes": true,
    "include_arcs": true,
    "include_centers": true,
    "detect_symmetry": true,
    "clear_existing": false,
    "dimension_layer": "MCP-DIM"
  },
  "include_screenshot": true
}
```

## Modes

- `minimal`: overall width/height plus circle/arc dimensions.
- `balanced`: adds chain dimensions for important X/Y coordinates and symmetric
  hole spacing. This is the default.
- `detailed`: keeps more feature coordinates, suitable for simpler parts where a
  denser drawing is acceptable.

## Placement strategy

The engine reads `LINE`, `LWPOLYLINE`, `POLYLINE`, `CIRCLE`, and `ARC` entities
in Model Space. It ignores dimensions and annotation entities, calculates the
part extents, then uses separate lanes outside the part for overall, chain, and
symmetry dimensions. Tiny chain segments are skipped to reduce text collisions.
Circle dimensions use diameter notation, arcs use radius notation, and center
marks are added when requested.

All generated annotation goes to `MCP-DIM` by default. `clear_existing=true`
removes previous generated annotation on that dedicated layer before rebuilding it. The operation is wrapped in one
AutoCAD UNDO group, so one undo removes the generated set.

## Limitations

This first implementation targets 2D manufacturing-style geometry. It does not
infer design intent from splines, hatches, solids, blocks, tolerances, GD&T, or
section/detail views. For crowded assembly drawings, pass `source_layers` to
restrict analysis to the part layers, or use `mode=minimal` first.
