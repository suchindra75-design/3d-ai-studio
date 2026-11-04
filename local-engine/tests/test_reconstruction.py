"""COLMAP runner and model helpers.

The runner tests use a tiny stand-in executable, so they run anywhere (CI has
no COLMAP). The real pipeline is exercised in backend/tests/test_photogrammetry.py
whenever COLMAP 4 is installed.
"""

import math
import os
import stat
import sys
import time
from pathlib import Path

import numpy as np
import psutil
import pytest

from ai3d_engine.reconstruction.colmap import (
    ColmapCancelledError,
    ColmapFailedError,
    ColmapNotFoundError,
    ColmapRunner,
    StepProgress,
    _progress,
    build_args,
    check_colmap,
)
from ai3d_engine.reconstruction.model import (
    RegisteredImage,
    estimate_up,
    read_text_model,
    upright_transform,
)

FAKE = """import os, sys, time
args = sys.argv[1:]
if args == ["-h"]:
    print("COLMAP {version} (Commit test)")
    sys.exit(0)
mode = os.environ.get("FAKE_COLMAP_MODE", "ok")
sys.stderr.write("I20261103 21:36:12.189494 0x7ff8 feature_extraction.cc:12] Starting\\n")
for i in range(1, 4):
    sys.stderr.write(f"I20261103 21:36:12.189494 0x7ff8 x.cc:1] Processed file [{{i}}/3]\\n")
    sys.stderr.flush()
if mode == "fail":
    sys.stderr.write("E20261103 21:36:12.189494 0x7ff8 x.cc:1] Not enough memory\\n")
    sys.exit(3)
if mode == "hang":
    with open(os.environ["FAKE_COLMAP_PID"], "w") as f:
        f.write(str(os.getpid()))
    time.sleep(60)
sys.exit(0)
"""


def _fake_colmap(tmp_path: Path, version: str = "4.2.1") -> Path:
    script = tmp_path / f"fake_colmap_{version}.py"
    script.write_text(FAKE.format(version=version))
    # A shell wrapper: shebang lines cannot hold paths with spaces (the venv may have one).
    path = tmp_path / f"colmap-{version}"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# -- arguments --------------------------------------------------------------------


def test_build_args_allows_only_known_subcommands_and_option_names() -> None:
    args = build_args("feature_extractor", {"database_path": Path("/w/db.db"), "X.use_gpu": False})
    assert args == [
        "feature_extractor",
        "--database_path",
        "/w/db.db",
        "--X.use_gpu",
        "0",
        "--log_target",
        "stderr",
        "--log_color",
        "0",
    ]
    with pytest.raises(ValueError, match="not allowed"):
        build_args("gui", {})
    with pytest.raises(ValueError, match="option name"):
        build_args("mapper", {"output_path; rm -rf /": "x"})
    with pytest.raises(ValueError, match="invalid value"):
        build_args("mapper", {"output_path": "a\nb"})


def test_progress_parsing() -> None:
    assert _progress("feature_extractor", "Processed file [3/12]", None) == 0.25
    # Matcher blocks: rows × columns.
    assert _progress("exhaustive_matcher", "Processing block [2/2, 1/2]", None) == 0.75
    assert _progress("mapper", "Registering image #5 (num_reg_frames=6)", 12) == 0.5
    assert _progress("mapper", "Registering image #5 (num_reg_frames=6)", None) is None
    assert _progress("feature_extractor", "Features: 3218 (SIFT)", None) is None


# -- running ------------------------------------------------------------------------


def test_runner_streams_progress_and_writes_the_log(tmp_path: Path) -> None:
    runner = ColmapRunner(_fake_colmap(tmp_path))
    seen: list[StepProgress] = []
    log = tmp_path / "colmap.log"
    runner.run(
        "feature_extractor",
        {"image_path": tmp_path},
        cwd=tmp_path,
        log_path=log,
        on_progress=seen.append,
    )
    assert [p.fraction for p in seen if p.fraction is not None] == pytest.approx([1 / 3, 2 / 3, 1])
    assert seen[0].line == "Starting"  # glog prefix removed
    text = log.read_text()
    assert text.startswith("$ colmap feature_extractor --image_path")
    assert "Processed file [3/3]" in text


def test_runner_reports_failures_with_the_log_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COLMAP_MODE", "fail")
    runner = ColmapRunner(_fake_colmap(tmp_path))
    with pytest.raises(ColmapFailedError) as caught:
        runner.run("mapper", {}, cwd=tmp_path, log_path=tmp_path / "log")
    assert caught.value.returncode == 3
    assert caught.value.tail[-1] == "Not enough memory"


def _wait_for_pid(path: Path) -> int:
    for _ in range(100):
        if path.exists() and path.read_text():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError("fake COLMAP did not start")


def test_cancelling_stops_the_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("FAKE_COLMAP_MODE", "hang")
    monkeypatch.setenv("FAKE_COLMAP_PID", str(pid_file))
    runner = ColmapRunner(_fake_colmap(tmp_path))
    started = time.monotonic()

    def cancelled() -> bool:
        return pid_file.exists() and time.monotonic() - started > 0.5

    with pytest.raises(ColmapCancelledError):
        runner.run("mapper", {}, cwd=tmp_path, log_path=tmp_path / "log", cancelled=cancelled)
    assert time.monotonic() - started < 10
    assert not psutil.pid_exists(_wait_for_pid(pid_file)) or (
        psutil.Process(_wait_for_pid(pid_file)).status() == psutil.STATUS_ZOMBIE
    )


def test_an_exception_from_the_cancel_check_also_stops_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E.g. the job runner shutting down raises inside the check."""
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("FAKE_COLMAP_MODE", "hang")
    monkeypatch.setenv("FAKE_COLMAP_PID", str(pid_file))
    runner = ColmapRunner(_fake_colmap(tmp_path))

    class StoppingError(Exception):
        pass

    def cancelled() -> bool:
        if pid_file.exists() and pid_file.read_text():
            raise StoppingError
        return False

    with pytest.raises(StoppingError):
        runner.run("mapper", {}, cwd=tmp_path, log_path=tmp_path / "log", cancelled=cancelled)
    pid = _wait_for_pid(pid_file)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_step_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_COLMAP_MODE", "hang")
    monkeypatch.setenv("FAKE_COLMAP_PID", str(tmp_path / "pid"))
    runner = ColmapRunner(_fake_colmap(tmp_path))
    with pytest.raises(ColmapFailedError) as caught:
        runner.run("mapper", {}, cwd=tmp_path, log_path=tmp_path / "log", timeout=0.5)
    assert caught.value.tail[-1] == "timed out"


def test_old_or_missing_colmap_is_refused(tmp_path: Path) -> None:
    old = _fake_colmap(tmp_path, version="3.9")
    assert "needs 4.0 or newer" in (check_colmap(old) or "")
    with pytest.raises(ColmapNotFoundError, match=r"needs 4\.0"):
        ColmapRunner(old)
    assert check_colmap(_fake_colmap(tmp_path, version="4.2.1")) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bit")
def test_missing_executable_message(tmp_path: Path) -> None:
    assert "did not report" in (check_colmap(tmp_path / "nope") or "")


# -- model --------------------------------------------------------------------------


def _look_at(eye: np.ndarray, target: np.ndarray) -> np.ndarray:
    """World-to-camera rotation for an upright camera (COLMAP: x right, y down, z forward)."""
    forward = (target - eye) / np.linalg.norm(target - eye)
    right = np.cross(forward, [0.0, 1.0, 0.0])
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    return np.array([right, down, forward])


def _orbit(up: np.ndarray, elevations: list[float]) -> list[RegisteredImage]:
    """Cameras orbiting the origin in a frame whose 'up' is ``up``."""
    # Rotation taking +Y to ``up`` (so the scene is tilted arbitrarily).
    y = np.array([0.0, 1.0, 0.0])
    axis = np.cross(y, up)
    angle = math.acos(float(np.clip(y @ up, -1, 1)))
    k = axis / (np.linalg.norm(axis) or 1)
    skew = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    tilt = np.eye(3) + math.sin(angle) * skew + (1 - math.cos(angle)) * skew @ skew
    images = []
    for n, elevation in enumerate(elevations):
        az = n * 0.7
        eye = np.array(
            [
                math.cos(elevation) * math.sin(az),
                math.sin(elevation),
                math.cos(elevation) * math.cos(az),
            ]
        )
        rotation = _look_at(eye * 3, np.zeros(3)) @ tilt.T
        images.append(RegisteredImage(f"{n}.jpg", rotation, tilt @ (eye * 3)))
    return images


def test_estimate_up_ignores_camera_tilt() -> None:
    up = np.array([0.3, 0.8, -0.52])
    up /= np.linalg.norm(up)
    # Cameras look down at 20–60°; the average camera "up" is far from vertical.
    images = _orbit(up, [math.radians(a) for a in (20, 35, 60, 25, 45, 30, 55, 40)])
    assert estimate_up(images) == pytest.approx(up, abs=1e-6)


def test_estimate_up_with_rolled_photos_uses_where_the_cameras_are() -> None:
    """Portrait and top-down shots (camera rolled): the x-axis rule cannot work.

    Found on a real capture (AliceVision Buddha) where it was 92° off.
    """
    up = np.array([0.0, 1.0, 0.0])
    rng = np.random.default_rng(3)
    images = []
    for n in range(24):
        az, elevation = 2 * math.pi * n / 24, math.radians(rng.uniform(15, 75))
        eye = 3 * np.array(
            [
                math.cos(elevation) * math.sin(az),
                math.sin(elevation),
                math.cos(elevation) * math.cos(az),
            ]
        )
        roll = rng.uniform(-math.pi, math.pi)  # any roll about the viewing axis
        c, s = math.cos(roll), math.sin(roll)
        rotation = np.array([[c, s, 0], [-s, c, 0], [0, 0, 1]]) @ _look_at(eye, np.zeros(3))
        images.append(RegisteredImage(f"{n}.jpg", rotation, eye))
    points = rng.normal(scale=0.2, size=(500, 3))
    estimated = estimate_up(images, points)
    angle = math.degrees(math.acos(float(np.clip(estimated @ up, -1, 1))))
    assert angle < 10
    # Upright photos still use the exact rule (see the test above).
    upright = _orbit(up, [math.radians(a) for a in (20, 35, 60, 25, 45, 30, 55, 40)])
    assert estimate_up(upright, points) == pytest.approx(up, abs=1e-6)


def test_upright_transform_puts_up_on_y_and_the_floor_at_zero() -> None:
    up = np.array([0.0, 0.0, 1.0])
    points = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (5.0, 6.0)])
    matrix = upright_transform(up, points)
    moved = points @ matrix[:3, :3].T + matrix[:3, 3]
    assert matrix[:3, :3] @ up == pytest.approx([0, 1, 0])
    assert np.percentile(moved[:, 1], 2) == pytest.approx(0, abs=1e-9)
    assert np.median(moved[:, 0]) == pytest.approx(0)
    # Scale is never changed.
    assert np.linalg.det(matrix[:3, :3]) == pytest.approx(1)


def test_read_text_model(tmp_path: Path) -> None:
    (tmp_path / "images.txt").write_text(
        "# comment\n"
        "1 1 0 0 0 0 0 2 1 a.jpg\n"
        "10 20 1 30 40 -1\n"
        "2 1 0 0 0 -1 0 2 1 b with space.jpg\n"
        "\n"  # an image without observations keeps its (empty) line
    )
    (tmp_path / "points3D.txt").write_text(
        "# id x y z r g b error track\n"
        "1 0 0 1 255 0 0 0.5 1 0 2 0\n"
        "2 1 1 1 0 255 0 1.5 1 1 2 1 3 4\n"
    )
    model = read_text_model(tmp_path)
    assert [image.name for image in model.images] == ["a.jpg", "b with space.jpg"]
    assert model.images[0].center == pytest.approx([0, 0, -2])
    assert model.images[1].center == pytest.approx([1, 0, -2])
    assert model.points.shape == (2, 3)
    assert model.colors.tolist() == [[255, 0, 0], [0, 255, 0]]
    assert model.mean_reprojection_error == pytest.approx(1.0)
    assert model.mean_track_length == pytest.approx(2.5)
