# Mesh inspection and repair

What the health inspection checks, how exactly, what each repair changes, and what
neither can do. Code: `backend/ai3d/processing/inspect.py` (D69) and
`backend/ai3d/processing/repair.py` (D70); in the app: the **Quality** tab →
**Health inspection**.

Inspecting never changes the model. Every repair runs only when asked, is
confirmed first, and saves a **new version**; the version it started from is never
changed and can be made current again from **Versions**.

## How it measures

- The model is read as stored: every mesh placed by its node, in glTF world space
  (metres when the model has a real-world scale, model units otherwise).
- Positions are **welded within 10⁻⁷ of the model's size** first. Texture seams and
  normal splits duplicate vertices on purpose; welding keeps them from looking like
  holes. Two points closer than that count as one.
- Each finding is marked:
  - **Exact**: computed on every triangle or edge. Exact for the stored
    floating-point geometry and the welding tolerance.
  - **Approximate**: a geometric rule with known blind spots (listed below).
  - **Sampled**: measured at sample points; between them nothing is measured.
- Counts are never cut short. Locations drawn on the model are capped at 2,000 per
  finding, spread evenly; the panel says "showing N of M".
- There is **no overall score**: a single number would hide which problems matter
  for what you want to do. Each category lists its findings, and its worst severity
  is what the panel shows first.

## The checks

| Category | Finding | Method | What it means and its limits |
|---|---|---|---|
| Geometry integrity | Invalid vertex positions | exact | NaN or infinite coordinates. |
| | Zero-area triangles | exact | A repeated corner (after welding) or area ≤ (10⁻⁸ × size)². |
| | Duplicate triangles | exact | The same three welded corners as another triangle, in any order. |
| | Self-intersections | approximate | Triangles passing through others. Candidate pairs are triangles sharing a cell of a uniform grid (cell 1.5 × the median triangle size). Each pair gets exact segment-triangle tests in both directions. Not detected: overlapping triangles lying in the same plane, and crossings between neighbours that share a corner. Triangles are registered in every cell their bounding box touches; long slivers whose box would cover over 4,096 cells, in the cells of points sampled a quarter cell apart over the triangle, which can miss a cell the triangle only grazes. In cells holding over 256 triangles, only entries fewer than 256 apart in the cell's list are paired. When either happens the report says the search was not complete. By default only up to 2 million triangles (see Memory and time); the report says when it was skipped. Tested against a check of every pair. |
| Topology | Non-manifold edges | exact | Edges used by three or more triangles. |
| | Holes and open borders | exact | Edges used by one triangle, grouped into connected openings, with each opening's edge count, length and centre. An open border (a plane, a terrain) is reported the same way as a hole. |
| | Inconsistent winding | exact | Edges whose two triangles run the same way along them, so one of them faces the wrong way. |
| | Separate pieces | exact | Pieces not connected by shared (welded) vertices; listed largest first with their ids, which the "remove chosen pieces" repair uses. |
| | Duplicate vertices | exact | Vertices identical in position, UVs, stored normal and colour to another in the same mesh. Vertices sharing only the position are seams, counted separately and not a problem. |
| Surface quality | Inside-out pieces | exact | Closed pieces with negative volume. |
| | Stored normals point against their triangles | exact | The mean stored normal of a triangle's corners points away from its winding. |
| | Invalid stored normals | exact | Not unit length (±1%) or not numbers. |
| | No stored normals | exact | Information: viewers compute them. |
| | Very dense model | exact | Over 1.5 million triangles. |
| | Very small triangles | exact | Under 1/10,000 of the median area; reported when over 0.1% of triangles. |
| UV and texture | Invalid material references | exact | From the file's own description: a mesh naming a material, or a material naming a texture or image, that does not exist. Reported even when the geometry cannot be read. |
| | Unused or duplicate materials | exact | Not used by any mesh; identical to another apart from the name. |
| | Missing or unreadable textures | exact | Images that cannot be decoded, or external files. |
| | Textures over 8192 px | exact | Many graphics cards cannot load them. |
| | Textured meshes without UVs | exact | A texture but no texture coordinates. |
| | Collapsed UV triangles | exact | Area on the model, none in texture space. |
| | Stretched UVs | exact | The share of the surface where a triangle's share of the texture differs from its share of the surface by more than 4×. |
| | Overlapping UV islands | approximate | Islands are triangles joined through vertices sharing position and UVs. Overlap is measured on a 512 × 512 grid of the texture square. Not detected: folds within one island, or overlaps smaller than a cell. Under 1% is not reported (cells on borders between islands hold both). |
| Fabrication (optional) | Not watertight | exact | Necessary for printing, **never sufficient**: watertightness alone is not reported as "printable". |
| | Several shells | exact | Closed pieces. |
| | Overhangs | exact angle | Triangles facing down more steeply than 45° (adjustable) from +Y as stored, excluding those on the build plate. Whether supports are needed depends on the printer and the orientation. |
| | Thin walls or features | sampled | From 2,000 area-weighted surface points (fixed seed), rays straight inwards along the triangle's normal and the smoothed surface normal, to the first surface facing the other way (within 60°); the longer reading is kept. Reported: minimum, 5th percentile and median thickness, and the points under the limit. The 5th percentile is the robust figure; the minimum is one sample. Can overestimate by up to about 40% where the two directions differ, and at sharp tips; misses thin spots between samples; not meaningful where the surface folds through itself. Tested on boxes and plates of known thickness, including a noisy one. In millimetres only when the model has a real-world scale; otherwise in model units, and the finding says so. "Minimum feature size" is the same measurement. |
| | Shells inside other shells | approximate | One oblique ray per closed piece, counting crossings of other pieces. A ray grazing an edge can miscount. Up to 200 closed pieces. |

Wall thickness and internal shells are measured only on watertight models: without a
closed surface, "inside" is not defined.

## The repairs

| Repair | Changes | Keeps | Can it change the shape? |
|---|---|---|---|
| Recalculate normals | Stored normals: computed from the triangles, area-weighted, smooth across texture seams | Positions, UVs, materials, colours, textures | No. Hard edges set by hand become smooth where vertices are shared. |
| Fix winding | Turns triangles round to agree with neighbours, outwards on closed pieces (trimesh) | Everything else | No. |
| Remove zero-area triangles | Deletes triangles with no area or invalid corners | The rest, with their UVs and materials | No visible change; a hole can open where such a triangle was the only link. |
| Remove duplicate triangles | Deletes repeats (the first is kept) | The rest | No. |
| Merge duplicate vertices | Merges vertices identical in position, UVs and normals | Seams, hard edges | No. |
| Fill small holes | Adds flat triangles in openings of 3 or 4 edges (trimesh) | Existing triangles | Yes, where the holes were. Not on textured or coloured meshes: the new triangles would have no UVs or colours. Larger openings are left as they are. |
| Separate pieces | Each piece (joined by shared edges) becomes its own part | All geometry, UVs, materials | No. |
| Remove chosen pieces | Deletes the pieces you ticked, and only those | Everything else | **Yes: destructive.** Confirmed separately. Refused when it would remove every piece. |
| Repair non-manifold edges | Removes duplicate and zero-area triangles where they make edges non-manifold | Everything else | No. Edges where three or more real surfaces meet are reported and left: any automatic fix would change the shape. |

Each repair's result says exactly what it changed (counts) or why it did nothing. A
repair that changes nothing makes no new version. A repair that fails (an error)
makes no version and leaves the source untouched; an interrupted one runs again
from the start, and only one version results (tested).

## Not supported

- Self-intersections that fold between neighbouring triangles, and coplanar
  overlaps.
- Automatic fixes for non-manifold edges where real surfaces meet, holes of more
  than 4 edges, self-intersections, thin walls, overhangs, UV overlaps or
  stretching: these are reported, not repaired.
- Point clouds and lines: only triangle meshes are inspected.
- Printing checks in millimetres without a real-world scale (set one in
  **Enhance → Set the real-world size**).
- Changes not yet saved as a version: the inspection reads the stored version.

## Memory and time

Measured figures are in [BENCHMARKS.md](BENCHMARKS.md#inspection-2026-11-08): on this Intel Mac,
about 30–50 s and up to 1 GB at 1 million triangles; 1–2 minutes and up to 5 GB at 10
million (with the printing checks); the full self-intersection search on 10 million
about 5 minutes. Large arrays are worked on a million triangles at a time where the
check allows; the model itself, welded, is held once.

Before it starts, an inspection checks free memory against 700 MiB + 350 bytes per
triangle + 300 bytes per vertex + decoded textures (about 5.7 GB for 10 million
triangles with 5 million vertices), and is refused with the numbers when that is
not free. The estimate is tested to stay above every measured peak. Inspections run
in the background, in a separate process, one at a time.

Self-intersections are searched by default only up to 2 million triangles (above
that, the search takes minutes: 2.5 at 5 million, 5 at 10 million). The API runs it on any size with
`"self_intersections": "always"`; the panel uses the default and says when it was skipped. UV overlaps are measured on meshes up to 5 million triangles; above
that the report says they were not checked.
