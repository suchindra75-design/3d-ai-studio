"""Running COLMAP safely.

Only the subcommands in :data:`SUBCOMMANDS` can run, every argument list is
built in this module from typed options, ``shell=True`` is never used, and the
process can be cancelled. Progress is read from COLMAP's log lines.
"""

import functools
import logging
import queue
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from ai3d_engine.runtime import find_tool

log = logging.getLogger(__name__)

SUBCOMMANDS = frozenset(
    {
        "feature_extractor",
        "exhaustive_matcher",
        "sequential_matcher",
        "mapper",
        "model_converter",
        "image_undistorter",
        "patch_match_stereo",
        "stereo_fusion",
        "delaunay_mesher",
        "mesh_texturer",
    }
)

_OPTION = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*$")
# "Processed file [3/24]", "Processing block [2/4, 1/4]", "Registering image #5 (num_reg_frames=4)"
_FRACTION = re.compile(r"\[(\d+)/(\d+)(?:,\s*(\d+)/(\d+))?\]")
_REGISTERED = re.compile(r"num_reg_(?:frames|images)=(\d+)")
_GLOG_PREFIX = re.compile(r"^[IWEF]\d{8} [\d:.]+ \S+ [\w.]+:\d+\] ")

TAIL_LINES = 40


class ColmapNotFoundError(RuntimeError):
    pass


class ColmapCancelledError(RuntimeError):
    pass


class ColmapFailedError(RuntimeError):
    def __init__(self, subcommand: str, returncode: int, tail: list[str]) -> None:
        super().__init__(f"colmap {subcommand} exited with code {returncode}")
        self.subcommand = subcommand
        self.returncode = returncode
        self.tail = tail


@dataclass(frozen=True, slots=True)
class StepProgress:
    """Fraction of the current step (0–1) when COLMAP reports one, plus the log line."""

    fraction: float | None
    line: str


ProgressCallback = Callable[[StepProgress], None]
CancelCheck = Callable[[], bool]


def build_args(
    subcommand: str, options: Mapping[str, str | int | float | bool | Path]
) -> list[str]:
    """``--Name value`` pairs for an allow-listed subcommand. Values are never parsed by a shell."""
    if subcommand not in SUBCOMMANDS:
        raise ValueError(f"COLMAP subcommand {subcommand!r} is not allowed")
    args = [subcommand]
    for name, value in options.items():
        if not _OPTION.match(name):
            raise ValueError(f"invalid COLMAP option name {name!r}")
        text = str(int(value)) if isinstance(value, bool) else str(value)
        if "\x00" in text or "\n" in text:
            raise ValueError(f"invalid value for COLMAP option {name!r}")
        args += [f"--{name}", text]
    # Plain log lines on stderr, so progress can be parsed.
    args += ["--log_target", "stderr", "--log_color", "0"]
    return args


def _progress(subcommand: str, line: str, total_images: int | None) -> float | None:
    if subcommand == "mapper":
        match = _REGISTERED.search(line)
        if match and total_images:
            return min(int(match.group(1)) / total_images, 1.0)
        return None
    match = _FRACTION.search(line)
    if not match:
        return None
    done, total = int(match.group(1)), int(match.group(2))
    if match.group(3):  # matcher blocks: [row/rows, col/cols]
        done_col, cols = int(match.group(3)), int(match.group(4))
        return ((done - 1) * cols + done_col) / (total * cols)
    return done / total if total else None


MIN_VERSION = (4, 0)
_VERSION = re.compile(r"COLMAP\s+(\d+)\.(\d+)")


@functools.cache
def colmap_version(executable: Path) -> tuple[int, int] | None:
    """(major, minor) reported by ``colmap -h``, or None if it cannot be read."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed executable and arguments
            [str(executable), "-h"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VERSION.search(completed.stdout + completed.stderr)
    return (int(match.group(1)), int(match.group(2))) if match else None


def check_colmap(executable: Path | None = None) -> str | None:
    """Why COLMAP cannot be used, or None when a supported version is installed."""
    path = executable or find_tool("colmap")
    if path is None:
        return "COLMAP is not installed. See docs/LOCAL_GPU_SETUP.md, or set AI3D_COLMAP_PATH."
    version = colmap_version(path)
    if version is None:
        return f"{path} did not report a COLMAP version."
    if version < MIN_VERSION:
        return (
            f"COLMAP {version[0]}.{version[1]} is installed; AI 3D Studio needs "
            f"{MIN_VERSION[0]}.{MIN_VERSION[1]} or newer (its option names changed in 4.0)."
        )
    return None


class ColmapRunner:
    def __init__(self, executable: Path | None = None) -> None:
        resolved = executable or find_tool("colmap")
        problem = check_colmap(resolved)
        if resolved is None or problem is not None:
            raise ColmapNotFoundError(problem or "COLMAP is not installed.")
        self.executable = resolved

    def run(
        self,
        subcommand: str,
        options: Mapping[str, str | int | float | bool | Path],
        *,
        cwd: Path,
        log_path: Path,
        on_progress: ProgressCallback | None = None,
        cancelled: CancelCheck | None = None,
        total_images: int | None = None,
        timeout: float | None = None,
    ) -> None:
        """Run one step to completion, streaming its log to ``log_path``."""
        command = [str(self.executable), *build_args(subcommand, options)]
        started = time.monotonic()
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        lines: queue.Queue[str | None] = queue.Queue()
        process = subprocess.Popen(  # noqa: S603 - allow-listed executable, code-built args
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            shell=False,
        )

        def pump() -> None:
            assert process.stdout is not None
            for raw in process.stdout:
                lines.put(raw)
            lines.put(None)

        reader = threading.Thread(target=pump, name=f"colmap-{subcommand}", daemon=True)
        reader.start()
        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(f"$ colmap {' '.join(command[1:])}\n")
                self._follow(
                    process,
                    lines,
                    log_file,
                    subcommand=subcommand,
                    tail=tail,
                    on_progress=on_progress,
                    cancelled=cancelled,
                    total_images=total_images,
                    deadline=None if timeout is None else started + timeout,
                )
        except BaseException:
            # Cancelled, timed out, or the caller is shutting down: never leave COLMAP running.
            if process.poll() is None:
                _stop(process)
            raise
        returncode = process.wait()
        reader.join(timeout=1)
        if returncode != 0:
            raise ColmapFailedError(subcommand, returncode, list(tail))

    @staticmethod
    def _follow(
        process: subprocess.Popen[str],
        lines: "queue.Queue[str | None]",
        log_file: TextIO,
        *,
        subcommand: str,
        tail: deque[str],
        on_progress: ProgressCallback | None,
        cancelled: CancelCheck | None,
        total_images: int | None,
        deadline: float | None,
    ) -> None:
        finished_reading = False
        while True:
            if cancelled is not None and cancelled():
                raise ColmapCancelledError(subcommand)
            if deadline is not None and time.monotonic() > deadline:
                raise ColmapFailedError(subcommand, -1, [*tail, "timed out"])
            try:
                raw = lines.get(timeout=0.25)
            except queue.Empty:
                if finished_reading and process.poll() is not None:
                    return
                continue
            if raw is None:
                finished_reading = True
                if process.poll() is not None:
                    return
                continue
            log_file.write(raw)
            line = _GLOG_PREFIX.sub("", raw.rstrip())
            if not line:
                continue
            tail.append(line)
            if on_progress is not None:
                on_progress(StepProgress(_progress(subcommand, line, total_images), line))


def _stop(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
