"""Time the backend's geometry work on large models (see docs/BENCHMARKS.md).

    uv run python infrastructure/scripts/benchmark_geometry.py OUT_DIR [--steps a,b] MODEL...

``--steps`` runs only those steps (after a full run: they reuse its imported files).

MODEL is ``synthetic:<triangles>[:<kind>]`` (a bumpy terrain made here, labelled as
synthetic in the results; kinds in ``synthetic()``) or a path to a real model file
(GLB, PLY, OBJ, STL…).
Every step runs in its own process, so its peak memory is its own (and that of
any process it starts, e.g. Blender, reported separately). Results are printed as
JSON lines and written to OUT_DIR/results.json; nothing here is estimated.

``AI3D_BENCHMARK_SAVE_VERTICES=FILE.npy`` saves the smoothed positions, to compare
two implementations point by point.
"""

# Heavy libraries are imported inside the measured steps, so each child process
# measures only what its step loads.
# ruff: noqa: PLC0415

from __future__ import annotations

import json
import math
import os
import resource
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

STEPS = (
    "import",
    "validate",
    "lod",
    "smooth_sharp",
    "smooth_taubin",
    "export_glb",
    "export_obj",
    "export_stl",
    "export_fbx",
)


KINDS = ("textured", "colours", "soup", "parts", "bare", "cube")


def synthetic(triangles: int, dest: Path, kind: str = "textured") -> None:
    """A bumpy heightfield of about ``triangles`` triangles, stored as ``kind``:

    textured (2048 px texture and UVs), colours (per-vertex colours), soup (every
    triangle its own three vertices, as STL-like files are), parts (400 separate
    meshes), bare (positions only: no normals, UVs or colours), cube (see
    ``noisy_cube``: a known true surface, for measuring smoothing)."""
    import numpy as np
    import trimesh
    from PIL import Image

    if kind == "cube":
        noisy_cube(triangles).export(dest, file_type="glb")
        return
    n = max(2, round(math.sqrt(triangles / 2)) + 1)  # n×n vertices → 2(n-1)² triangles
    u, v = np.meshgrid(np.linspace(0, 1, n), np.linspace(0, 1, n))
    height = 0.05 * np.sin(23 * u) * np.cos(17 * v) + 0.02 * np.sin(91 * u + 37 * v)
    vertices = np.stack([u.ravel() - 0.5, height.ravel(), v.ravel() - 0.5], axis=1)
    index = np.arange(n * n).reshape(n, n)
    a, b = index[:-1, :-1].ravel(), index[:-1, 1:].ravel()
    c, d = index[1:, :-1].ravel(), index[1:, 1:].ravel()
    faces = np.concatenate([np.stack([a, c, b], 1), np.stack([b, c, d], 1)])
    rng = np.random.default_rng(0)
    uv = np.stack([u.ravel(), v.ravel()], 1)
    if kind == "parts":
        scene = trimesh.Scene()
        # Patches of neighbouring cells (20 × 20 blocks), so each part is a surface.
        cell_row, cell_col = (faces.min(axis=1) // n), (faces.min(axis=1) % n)
        block = (cell_row * 20 // (n - 1)) * 20 + (cell_col * 20 // (n - 1))
        for k in range(400):
            chunk = np.flatnonzero(block == k)
            if not len(chunk):
                continue
            used, local = np.unique(faces[chunk], return_inverse=True)
            part = trimesh.Trimesh(vertices[used], local.reshape(-1, 3), process=False)
            scene.add_geometry(part, geom_name=f"part{k}", node_name=f"part{k}")
        scene.export(dest, file_type="glb")
        return
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    if kind == "textured":
        image = Image.fromarray(rng.integers(40, 220, (2048, 2048, 3), dtype=np.uint8))
        mesh.visual = trimesh.visual.TextureVisuals(uv=uv, image=image)
    elif kind == "colours":
        colours = np.concatenate(
            [(uv * 255).astype(np.uint8), np.full((len(uv), 2), 255, dtype=np.uint8)], axis=1
        )
        mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=colours)
    elif kind == "soup":
        mesh = trimesh.Trimesh(
            vertices=vertices[faces].reshape(-1, 3),
            faces=np.arange(len(faces) * 3).reshape(-1, 3),
            process=False,
        )
    trimesh.Scene(mesh).export(dest, file_type="glb", include_normals=kind != "bare")


def _position_noise(points: Any, size: float) -> Any:
    """Noise that depends only on the position (so a point shared by two sides of a
    seam gets the same), uniform in ±``size``·√3 per axis (standard deviation
    ``size``), from an integer hash of the position."""
    import numpy as np

    keys = np.round(points * 1e7).astype(np.int64)
    mixed = keys[:, 0] * 73856093 ^ keys[:, 1] * 19349663 ^ keys[:, 2] * 83492791
    out = np.empty_like(points)
    for axis in range(3):
        h = (mixed + np.int64(0x9E3779B97F4A7C15 >> 2) * (axis + 1)) & 0xFFFFFFFFFFFF
        h = (h ^ (h >> 17)) * 0xED5AD4BB & 0xFFFFFFFFFFFF
        h ^= h >> 11
        out[:, axis] = (h % 1_000_003) / 1_000_002 * 2 - 1
    return out * size * math.sqrt(3)


def noisy_cube(triangles: int) -> Any:
    """A unit cube (sides at ±0.5) of about ``triangles`` triangles, each side its own
    textured grid (so the cube edges are texture seams), with noise of 13% of a
    triangle's side (as in the unit tests). Its true surface is known, so smoothing
    can be scored: distance to it overall, near edges and at corners."""
    import numpy as np
    import trimesh
    from PIL import Image

    n = max(2, round(math.sqrt(triangles / 12)) + 1)  # per side: n×n vertices
    s, t = (g.ravel() for g in np.meshgrid(np.linspace(-0.5, 0.5, n), np.linspace(-0.5, 0.5, n)))
    index = np.arange(n * n).reshape(n, n)
    a, b = index[:-1, :-1].ravel(), index[:-1, 1:].ravel()
    c, d = index[1:, :-1].ravel(), index[1:, 1:].ravel()
    grid_faces = np.concatenate([np.stack([a, b, c], 1), np.stack([b, d, c], 1)])
    half = np.full_like(s, 0.5)
    sides = [
        np.stack([half, s, t], 1), np.stack([-half, t, s], 1),
        np.stack([t, half, s], 1), np.stack([s, -half, t], 1),
        np.stack([s, t, half], 1), np.stack([t, s, -half], 1),
    ]  # fmt: skip
    vertices, faces, uv = [], [], []
    for k, positions in enumerate(sides):
        side_faces = grid_faces
        # Wind each side so its normals point out of the cube.
        p0, p1, p2 = positions[side_faces[0]]
        if np.dot(np.cross(p1 - p0, p2 - p0), p0) < 0:
            side_faces = side_faces[:, [0, 2, 1]]
        vertices.append(positions)
        faces.append(side_faces + k * n * n)
        uv.append(np.stack([(k % 3 + (s + 0.5)) / 3, (k // 3 + (t + 0.5)) / 2], 1))
    mesh = trimesh.Trimesh(np.concatenate(vertices), np.concatenate(faces), process=False)
    mesh.vertices = mesh.vertices + _position_noise(mesh.vertices, 0.13 / (n - 1))
    image = Image.fromarray(np.random.default_rng(1).integers(40, 220, (256, 384, 3), np.uint8))
    mesh.visual = trimesh.visual.TextureVisuals(uv=np.concatenate(uv), image=image)
    return trimesh.Scene(mesh)


def cube_errors(vertices: Any) -> dict[str, float]:
    """Distance of each vertex to the true cube surface, as a share of its size."""
    import numpy as np

    q = np.abs(vertices) - 0.5
    distance = np.abs(np.linalg.norm(np.maximum(q, 0), axis=1) + np.minimum(q.max(axis=1), 0))
    near = np.sort(np.abs(vertices), axis=1)
    return {
        "rms": float(np.sqrt((distance**2).mean())),
        "edges": float(distance[near[:, 1] > 0.45].mean()),  # two coordinates at a side
        "corners": float(distance[near[:, 0] > 0.45].mean()),
    }


def step(name: str, source: Path, work: Path) -> dict[str, Any]:
    """One measured step, run in a child process (see ``main``)."""
    from ai3d.models.enums import ExportFormat
    from ai3d.processing import importing, lod
    from ai3d.processing.mesh import load_model_scene, validate_mesh_file

    glb = work / "model.glb"
    started = time.perf_counter()
    facts: dict[str, Any] = {}
    if name == "import":
        converted = importing.convert(source, source.name, work)
        if converted.glb != glb:
            converted.glb.replace(glb)
        facts["glb_mb"] = round(glb.stat().st_size / 1e6, 1)
    elif name == "validate":
        report = validate_mesh_file(glb)
        facts["triangles"] = report.metrics.get("triangle_count")
        facts["usable"] = report.usable
    elif name == "lod":
        levels = lod.build_levels(load_model_scene(glb))
        facts["levels"] = [
            {
                "triangles": level.triangles,
                "seconds": round(level.seconds, 1),
                "mb": round(len(level.glb) / 1e6, 1),
                "mean_shift_pct": round(level.mean_shift * 100, 3),
            }
            for level in levels
        ]
    elif name.startswith("smooth_"):
        facts.update(_smooth(name, glb, work))
    elif name.startswith("export_"):
        from ai3d.services.exports import _write_checked

        # The app's own path: write, free the model, re-open and compare.
        fmt = ExportFormat(name.removeprefix("export_"))
        out = work / f"export.{fmt.value}"
        _, validation = _write_checked(glb, fmt, out, lambda _scene: None)
        facts["mb"] = round(out.stat().st_size / 1e6, 1)
        facts["reopened_triangles"] = validation.get("triangles")
    seconds = time.perf_counter() - started
    children = _peak_mb(resource.RUSAGE_CHILDREN)
    if children:  # e.g. Blender writing FBX, in its own process
        facts["peak_mb_child_process"] = children
    return {"step": name, "seconds": round(seconds, 1), "peak_mb": _peak_mb(), **facts}


def _smooth(name: str, glb: Path, work: Path) -> dict[str, Any]:
    """Smooth with 10 passes and check what must not change (see docs/BENCHMARKS.md)."""
    import numpy as np
    import trimesh

    from ai3d.processing import geometry
    from ai3d.processing.mesh import load_model_scene

    facts: dict[str, Any] = {}
    scene = load_model_scene(glb)
    meshes = [g for g in scene.geometry.values() if isinstance(g, trimesh.Trimesh)]
    uv = [np.array(getattr(m.visual, "uv", None)) for m in meshes]
    colours = [np.array(getattr(m.visual, "vertex_colors", None)) for m in meshes]
    welded = [len(trimesh.grouping.unique_rows(m.vertices)[0]) for m in meshes]
    triangles = [len(m.faces) for m in meshes]
    cube = work.name.endswith("-cube")
    if cube:
        facts["noise"] = cube_errors(meshes[0].vertices)
    method = "sharp" if name == "smooth_sharp" else "taubin"
    import psutil

    # Memory in use with the model loaded: the smoothing's own need is the peak above it.
    facts["rss_mb_before_smoothing"] = round(psutil.Process().memory_info().rss / 1e6)
    result = geometry.smooth(scene, 10, method)
    # Peak memory of the smoothing itself (the checks below come after it).
    facts["peak_mb_smoothing"] = _peak_mb()
    facts["detail"] = result.detail
    facts["uv_unchanged"] = all(
        np.array_equal(np.array(getattr(m.visual, "uv", None)), u)
        for m, u in zip(meshes, uv, strict=True)
    )
    facts["colours_unchanged"] = all(
        np.array_equal(np.array(getattr(m.visual, "vertex_colors", None)), c)
        for m, c in zip(meshes, colours, strict=True)
    )
    # Seams stay closed: points that were together are still together.
    facts["seams_closed"] = welded == [
        len(trimesh.grouping.unique_rows(m.vertices)[0]) for m in meshes
    ]
    if cube:
        facts["after"] = cube_errors(meshes[0].vertices)
    facts["triangles_unchanged"] = all(
        len(m.faces) == t for m, t in zip(meshes, triangles, strict=True)
    )
    # Normals are saved for the smoothed surface when the model had them.
    saved = scene.export(file_type="glb")
    with glb.open("rb") as original:
        had_normals = _has_normals(original.read(1 << 24))  # the header is at the start
    facts["normals_kept"] = _has_normals(saved) or not had_normals
    del saved
    keep = os.environ.get("AI3D_BENCHMARK_SAVE_VERTICES")
    if keep:  # to compare two implementations' results point by point
        np.save(keep, np.concatenate([np.asarray(m.vertices) for m in meshes]))
    return facts


def _has_normals(glb: bytes) -> bool:
    """Whether any primitive of a GLB stores normals (from its JSON header)."""
    length = int.from_bytes(glb[12:16], "little")
    document = json.loads(glb[20 : 20 + length])
    return any(
        "NORMAL" in primitive.get("attributes", {})
        for mesh in document.get("meshes", [])
        for primitive in mesh.get("primitives", [])
    )


def _peak_mb(who: int = resource.RUSAGE_SELF) -> int:
    """Peak resident memory so far, in MB (10⁶ bytes): this process's, or (with
    ``RUSAGE_CHILDREN``) the largest of the processes it started that have ended."""
    peak = resource.getrusage(who).ru_maxrss
    return round(peak / 1e6 if sys.platform == "darwin" else peak / 1e3)  # bytes vs KiB


def main(argv: list[str]) -> int:
    if len(argv) >= 4 and argv[0] == "--step":
        print(json.dumps(step(argv[1], Path(argv[2]), Path(argv[3]))))
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2
    out = Path(argv[0]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    steps, specs = list(STEPS), argv[1:]
    if specs and specs[0] == "--steps":
        steps, specs = specs[1].split(","), specs[2:]
    results: list[dict[str, Any]] = []
    for spec in specs:
        if spec.startswith("synthetic:"):
            _, count, *rest = spec.split(":")
            triangles, kind = int(count), (rest[0] if rest else "textured")
            if kind not in KINDS:
                print(f"unknown kind {kind!r}; one of {', '.join(KINDS)}")
                return 2
            shape = "noisy cube" if kind == "cube" else "terrain"
            label = f"synthetic {shape}, {triangles:,} triangles" + (
                "" if kind in {"textured", "cube"} else f", {kind}"
            )
            work = out / (f"synthetic-{triangles}" + ("" if kind == "textured" else f"-{kind}"))
            work.mkdir(exist_ok=True)
            source = work / "source.glb"
            if not source.exists():
                synthetic(triangles, source, kind)
        else:
            source = Path(spec).resolve()
            label = source.name
            work = out / source.stem.replace(".", "-")
            work.mkdir(exist_ok=True)
        for name in steps:
            run = subprocess.run(  # noqa: S603 - this script, with our own arguments
                [sys.executable, __file__, "--step", name, str(source), str(work)],
                capture_output=True,
                text=True,
                check=False,
            )
            if run.returncode != 0:
                tail = (run.stderr or run.stdout).strip().splitlines()[-3:]
                record: dict[str, Any] = {"step": name, "failed": " | ".join(tail)}
            else:
                record = json.loads(run.stdout.strip().splitlines()[-1])
            record["model"] = label
            print(json.dumps(record), flush=True)
            results.append(record)
            if name == "import" and "failed" in record:
                break
    name = "results.json" if steps == list(STEPS) else f"results-{'-'.join(steps)}.json"
    (out / name).write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
