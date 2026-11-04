"""The restricted Blender runner (allow-listed commands, timeouts, cancellation)."""

import json
from pathlib import Path

import pytest

from ai3d_engine import blender
from ai3d_engine.blender import (
    BlenderCancelledError,
    BlenderFailedError,
    BlenderNotFoundError,
    build_args,
    check_blender,
    run_blender,
)

PROBLEM = check_blender()
needs_blender = pytest.mark.skipif(PROBLEM is not None, reason=f"Blender: {PROBLEM}")


def test_only_allow_listed_commands_and_the_repo_script(tmp_path: Path) -> None:
    args = build_args("inspect", tmp_path / "r.json", tmp_path / "o.json")
    assert args[:6] == [
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(blender.SCRIPT),
    ]
    assert args[6:] == ["--", "inspect", str(tmp_path / "r.json"), str(tmp_path / "o.json")]
    assert blender.SCRIPT.is_file()
    with pytest.raises(ValueError, match="not allowed"):
        build_args("python-expr", tmp_path / "r.json", tmp_path / "o.json")


def test_missing_blender_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(blender, "find_tool", lambda name: None)
    assert check_blender() == (
        "Needs Blender 4.2 or newer (blender.org), which was not found on this computer."
    )
    with pytest.raises(BlenderNotFoundError):
        run_blender("inspect", {"input": "x.glb"}, workdir=tmp_path)


@needs_blender
def test_version_is_read() -> None:
    version = blender.blender_version()
    assert version is not None
    assert version.startswith("Blender ")


@needs_blender
def test_script_errors_come_back_as_messages(tmp_path: Path) -> None:
    with pytest.raises(BlenderFailedError) as excinfo:
        run_blender("inspect", {"input": str(tmp_path / "missing.glb")}, workdir=tmp_path)
    assert excinfo.value.message == "The file does not exist."
    result = json.loads((tmp_path / "blender-inspect-result.json").read_text())
    assert result["ok"] is False
    assert result["blender"]


@needs_blender
def test_a_run_can_be_cancelled_and_times_out(tmp_path: Path) -> None:
    with pytest.raises(BlenderCancelledError):
        run_blender(
            "inspect", {"input": "x.glb"}, workdir=tmp_path, cancel=lambda: True, poll_seconds=0.1
        )
    with pytest.raises(BlenderFailedError, match="did not finish within"):
        run_blender("inspect", {"input": "x.glb"}, workdir=tmp_path, timeout=0.2, poll_seconds=0.1)
