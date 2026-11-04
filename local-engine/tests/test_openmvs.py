"""The OpenMVS runner: finding the programs, arguments, failures, cancel, timeout.

Stand-in programs (small Python scripts named like the OpenMVS executables)
replace OpenMVS here; the real programs run in backend/tests/test_photogrammetry.py
and test_real_captures.py when AI3D_OPENMVS_DIR is set.
"""

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from ai3d_engine.reconstruction import openmvs

# The shebang avoids sys.executable: shebang lines cannot contain spaces.
STAND_IN = """#!/usr/bin/env python3
import sys, time, pathlib
args = sys.argv[1:]
mode = pathlib.Path(__file__).with_name("mode").read_text().strip()
print("args:", " ".join(args), flush=True)
if mode == "slow":
    time.sleep(30)
if mode == "fail":
    print("error: something broke", flush=True)
    sys.exit(2)
out = None
if "-o" in args:
    out = args[args.index("-o") + 1]
elif pathlib.Path(sys.argv[0]).name == "DensifyPointCloud":
    out = str(pathlib.Path(args[args.index("-w") + 1]) / "scene_dense.mvs")
if out and mode != "no-output":
    pathlib.Path(out).write_text("ok")
"""


@pytest.fixture
def programs(tmp_path: Path) -> Iterator[Path]:
    folder = tmp_path / "bin"
    folder.mkdir()
    for name in openmvs.PROGRAMS:
        path = folder / name
        path.write_text(STAND_IN)
        path.chmod(0o755)
    (folder / "mode").write_text("ok")
    openmvs.configure(folder)
    yield folder
    openmvs.configure(None)


def _runner(tmp_path: Path) -> openmvs.OpenMVSRunner:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return openmvs.OpenMVSRunner(work, log_path=tmp_path / "openmvs.log")


def never() -> bool:
    return False


def test_all_programs_must_be_present(tmp_path: Path) -> None:
    folder = tmp_path / "partial"
    folder.mkdir()
    (folder / "DensifyPointCloud").write_text("")
    openmvs.configure(folder)
    try:
        assert openmvs.find_openmvs() is None
        assert "OpenMVS was not found" in (openmvs.check_openmvs() or "")
        with pytest.raises(openmvs.OpenMVSNotFoundError):
            openmvs.OpenMVSRunner(tmp_path, log_path=tmp_path / "log")
    finally:
        openmvs.configure(None)


@pytest.mark.skipif(os.name == "nt", reason="stand-in programs use a shebang")
def test_the_steps_run_with_code_built_arguments(programs: Path, tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    dense = tmp_path / "dense"
    (dense / "images").mkdir(parents=True)
    scene = runner.import_colmap(dense, cancelled=never, timeout=30)
    dense_scene = runner.densify(scene, resolution_level=2, cancelled=never, timeout=30)
    mesh = runner.mesh(dense_scene, decimate=0.5, cancelled=never, timeout=30)
    textured = runner.texture(dense_scene, mesh, cancelled=never, timeout=30)
    assert textured.name == "scene_textured.obj"
    log = (tmp_path / "openmvs.log").read_text()
    assert "--resolution-level 2" in log
    assert "--decimate 0.5" in log
    assert "--export-type obj" in log
    assert f"-w {runner.workspace}" in log


def test_arguments_are_checked_before_anything_runs(tmp_path: Path, programs: Path) -> None:
    runner = _runner(tmp_path)
    with pytest.raises(ValueError, match="resolution_level"):
        runner.densify(tmp_path / "s.mvs", resolution_level=7, cancelled=never, timeout=1)
    with pytest.raises(ValueError, match="decimate"):
        runner.mesh(tmp_path / "s.mvs", decimate=0, cancelled=never, timeout=1)
    with pytest.raises(ValueError, match="not allowed"):
        runner._run("sh", ["-c", "true"], cancelled=never, timeout=1)
    with pytest.raises(ValueError, match="invalid"):
        runner._run("TextureMesh", ["a\nb"], cancelled=never, timeout=1)


@pytest.mark.skipif(os.name == "nt", reason="stand-in programs use a shebang")
@pytest.mark.parametrize(
    ("mode", "message"), [("fail", "exited with code 2"), ("no-output", "did not write")]
)
def test_failures_are_reported_with_the_log_tail(
    programs: Path, tmp_path: Path, mode: str, message: str
) -> None:
    (programs / "mode").write_text(mode)
    runner = _runner(tmp_path)
    with pytest.raises(openmvs.OpenMVSFailedError, match=message) as caught:
        runner.import_colmap(tmp_path, cancelled=never, timeout=30)
    if mode == "fail":
        assert "error: something broke" in caught.value.tail


@pytest.mark.skipif(os.name == "nt", reason="stand-in programs use a shebang")
def test_cancel_and_timeout_stop_the_program(programs: Path, tmp_path: Path) -> None:
    (programs / "mode").write_text("slow")
    runner = _runner(tmp_path)
    started = time.monotonic()
    with pytest.raises(openmvs.OpenMVSCancelledError):
        runner.densify(
            tmp_path / "s.mvs",
            resolution_level=1,
            cancelled=lambda: time.monotonic() - started > 0.5,
            timeout=60,
        )
    assert time.monotonic() - started < 10
    started = time.monotonic()
    with pytest.raises(openmvs.OpenMVSFailedError, match="did not finish"):
        runner.densify(tmp_path / "s.mvs", resolution_level=1, cancelled=never, timeout=0.5)
    assert time.monotonic() - started < 10
