"""Running Blender safely, headless.

Blender is only ever started as::

    <blender found by find_tool("blender")> --background --factory-startup
        --python-exit-code 1 --python <the repo's blender_scripts/<script>.py>
        -- <command> <request.json> <result.json>

``command`` is one of :data:`COMMANDS`; the script is ``ai3d_blender.py``, or
``ai3d_scene.py`` for ``render_scene`` (CGI shots made from a prompt). The request
is JSON written here from typed values (file paths, names from fixed sets and
numbers). No user-provided script text or shell is involved, ``shell=True`` is
never used, runs have a timeout, and a caller can cancel a run.
"""

import functools
import json
import logging
import re
import subprocess
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ai3d_engine.runtime import find_tool, run_tool

log = logging.getLogger(__name__)

SCRIPT = Path(__file__).resolve().parent / "blender_scripts" / "ai3d_blender.py"
SCENE_SCRIPT = SCRIPT.with_name("ai3d_scene.py")
COMMANDS = frozenset({"inspect", "export_fbx", "import_fbx", "bake", "remesh", "render_scene"})
# The script uses the Blender 4.x Python API (Principled BSDF socket names, EMIT bake).
MIN_VERSION = (4, 2)
TAIL_LINES = 30
_VERSION = re.compile(r"^Blender (\d+)\.(\d+)(?:\.(\d+))?")

CancelCheck = Callable[[], bool]


class BlenderNotFoundError(RuntimeError):
    pass


class BlenderCancelledError(RuntimeError):
    pass


class BlenderFailedError(RuntimeError):
    """The run failed; ``message`` is the script's own explanation when it gave one."""

    def __init__(self, command: str, message: str, tail: list[str]) -> None:
        super().__init__(f"blender {command}: {message}")
        self.command = command
        self.message = message
        self.tail = tail


def find_blender() -> Path | None:
    return find_tool("blender")


@functools.cache
def _version_of(executable: Path) -> tuple[str, tuple[int, int, int]] | None:
    output = run_tool("blender", ["--background", "--factory-startup", "--version"], timeout=120)
    for line in (output or "").splitlines():
        match = _VERSION.match(line.strip())
        if match:
            parts = (int(match[1]), int(match[2]), int(match[3] or 0))
            return line.strip(), parts
    log.warning("could not read the Blender version", extra={"executable": str(executable)})
    return None


def blender_version() -> str | None:
    """E.g. "Blender 4.5.15 LTS", or None when Blender is missing or does not start."""
    executable = find_blender()
    found = _version_of(executable) if executable is not None else None
    return found[0] if found else None


def check_blender() -> str | None:
    """Why Blender cannot be used, or None when it can."""
    executable = find_blender()
    if executable is None:
        return "Needs Blender 4.2 or newer (blender.org), which was not found on this computer."
    found = _version_of(executable)
    if found is None:
        return f"Blender at {executable} did not start."
    if found[1][:2] < MIN_VERSION:
        return f"Needs Blender 4.2 or newer; found {found[0]}."
    return None


def build_args(command: str, request_path: Path, result_path: Path) -> list[str]:
    if command not in COMMANDS:
        raise ValueError(f"Blender command {command!r} is not allowed")
    return [
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(SCENE_SCRIPT if command == "render_scene" else SCRIPT),
        "--",
        command,
        str(request_path),
        str(result_path),
    ]


def run_blender(
    command: str,
    request: Mapping[str, Any],
    *,
    workdir: Path,
    timeout: float = 600.0,
    cancel: CancelCheck | None = None,
    poll_seconds: float = 1.0,
) -> dict[str, Any]:
    """Run one script command and return its JSON result (``ok`` is True).

    Raises BlenderNotFoundError, BlenderCancelledError, or BlenderFailedError
    (non-zero exit, timeout, or the script reporting a problem).
    """
    executable = find_blender()
    if executable is None:
        raise BlenderNotFoundError("Blender was not found.")
    workdir.mkdir(parents=True, exist_ok=True)
    request_path = workdir / f"blender-{command}-request.json"
    result_path = workdir / f"blender-{command}-result.json"
    log_path = workdir / f"blender-{command}.log"
    result_path.unlink(missing_ok=True)
    request_path.write_text(json.dumps(dict(request)), encoding="utf-8")
    args = [str(executable), *build_args(command, request_path, result_path)]
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        process = subprocess.Popen(  # noqa: S603 - fixed executable + code-built args
            args,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=workdir,
            shell=False,
        )
        try:
            while True:
                try:
                    returncode = process.wait(timeout=poll_seconds)
                    break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel():
                        raise BlenderCancelledError(command) from None
                    if time.monotonic() - started > timeout:
                        raise BlenderFailedError(
                            command, f"Blender did not finish within {timeout:g} s.", []
                        ) from None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    tail = list(
        deque(log_path.read_text(encoding="utf-8", errors="replace").splitlines(), TAIL_LINES)
    )
    result: dict[str, Any] | None = None
    if result_path.is_file():
        try:
            loaded = json.loads(result_path.read_text(encoding="utf-8"))
            result = loaded if isinstance(loaded, dict) else None
        except ValueError:
            result = None
    if result is not None and result.get("ok") is True and returncode == 0:
        result["seconds_wall"] = round(time.monotonic() - started, 3)
        return result
    message = (
        str(result.get("error"))
        if result and result.get("error")
        else (f"Blender exited with code {returncode}.")
    )
    log.info("blender run failed", extra={"command": command, "tail": tail[-5:]})
    raise BlenderFailedError(command, message, tail)
