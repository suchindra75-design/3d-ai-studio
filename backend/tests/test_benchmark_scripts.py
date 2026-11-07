"""The helper scripts used for benchmarks and hardware reports (docs/HARDWARE_TESTING.md)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import numpy as np

SCRIPTS = Path(__file__).resolve().parents[2] / "infrastructure" / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_the_machine_report_describes_this_computer(tmp_path: Path) -> None:
    report = _load("machine_report")
    out = tmp_path / "machine.json"
    assert report.main([str(out)]) == 0
    facts = json.loads(out.read_text())
    assert facts["memory_gib"] > 0
    assert facts["cores"]["logical"] >= 1
    assert facts["os"]
    assert facts["python"]
    assert isinstance(facts["gpus"], list)
    assert report._run(["no-such-command-ai3d"]) == ""  # missing tools are left empty


def test_the_noisy_cube_has_closed_seams_outward_faces_and_a_known_surface() -> None:
    bench = _load("benchmark_geometry")
    import trimesh  # noqa: PLC0415

    scene = bench.noisy_cube(20_000)
    (mesh,) = scene.geometry.values()
    assert 18_000 < len(mesh.faces) < 22_000
    welded = mesh.copy()
    welded.merge_vertices(merge_tex=True, merge_norm=True)
    assert welded.is_watertight  # the sides meet: seams are only in the texture
    assert welded.volume > 0  # faces point out
    assert abs(welded.volume - 1) < 0.01
    errors = bench.cube_errors(np.asarray(mesh.vertices))
    side = 1 / (round(np.sqrt(20_000 / 12)) + 1 - 1)
    assert 0.05 * side < errors["rms"] < 0.2 * side  # noise of 13% of a triangle side
    assert isinstance(mesh.visual, trimesh.visual.TextureVisuals)
