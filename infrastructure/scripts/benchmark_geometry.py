"""Time the backend's geometry work on large models (see docs/BENCHMARKS.md).

    uv run python infrastructure/scripts/benchmark_geometry.py OUT_DIR [--steps a,b] MODEL...

``--steps`` runs only those steps (after a full run: they reuse its imported files).

MODEL is ``synthetic:<triangles>`` (a textured, bumpy terrain made here, labelled as
synthetic in the results) or a path to a real model file (GLB, PLY, OBJ, STL…).
Every step runs in its own process, so its peak memory is its own. Results are
printed as JSON lines and written to OUT_DIR/results.json; nothing here is estimated.
"""

# Heavy libraries are imported inside the measured steps, so each child process
# measures only what its step loads.
# ruff: noqa: PLC0415

from __future__ import annotations

import json
import math
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


def synthetic(triangles: int, dest: Path) -> None:
    """A heightfield terrain of about ``triangles`` triangles with a 2048 px texture."""
    import numpy as np
    import trimesh
    from PIL import Image

    n = max(2, round(math.sqrt(triangles / 2)) + 1)  # n×n vertices → 2(n-1)² triangles
    u, v = np.meshgrid(np.linspace(0, 1, n), np.linspace(0, 1, n))
    height = 0.05 * np.sin(23 * u) * np.cos(17 * v) + 0.02 * np.sin(91 * u + 37 * v)
    vertices = np.stack([u.ravel() - 0.5, height.ravel(), v.ravel() - 0.5], axis=1)
    index = np.arange(n * n).reshape(n, n)
    a, b = index[:-1, :-1].ravel(), index[:-1, 1:].ravel()
    c, d = index[1:, :-1].ravel(), index[1:, 1:].ravel()
    faces = np.concatenate([np.stack([a, c, b], 1), np.stack([b, c, d], 1)])
    rng = np.random.default_rng(0)
    image = Image.fromarray(rng.integers(40, 220, (2048, 2048, 3), dtype=np.uint8))
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.visual = trimesh.visual.TextureVisuals(uv=np.stack([u.ravel(), v.ravel()], 1), image=image)
    trimesh.Scene(mesh).export(dest, file_type="glb")


def step(name: str, source: Path, work: Path) -> dict[str, Any]:
    """One measured step, run in a child process (see ``main``)."""
    from ai3d.models.enums import ExportFormat
    from ai3d.processing import exporters, geometry, importing, lod
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
        scene = load_model_scene(glb)
        method = "sharp" if name == "smooth_sharp" else "taubin"
        result = geometry.smooth(scene, 10, method)
        facts["detail"] = result.detail
    elif name.startswith("export_"):
        fmt = ExportFormat(name.removeprefix("export_"))
        scene = load_model_scene(glb)
        source_facts = exporters.scene_facts(scene)
        out = work / f"export.{fmt.value}"
        exporters.write(scene, fmt, out)
        written = time.perf_counter() - started
        validation = exporters.validate(out, fmt, source_facts)
        facts["write_s"] = round(written, 1)
        facts["mb"] = round(out.stat().st_size / 1e6, 1)
        facts["reopened_triangles"] = validation.get("triangles")
    seconds = time.perf_counter() - started
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mb = peak / 1e6 if sys.platform == "darwin" else peak / 1e3  # bytes vs KiB
    return {"step": name, "seconds": round(seconds, 1), "peak_mb": round(peak_mb), **facts}


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
            triangles = int(spec.split(":", 1)[1])
            label = f"synthetic terrain, {triangles:,} triangles"
            work = out / f"synthetic-{triangles}"
            work.mkdir(exist_ok=True)
            source = work / "source.glb"
            if not source.exists():
                synthetic(triangles, source)
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
