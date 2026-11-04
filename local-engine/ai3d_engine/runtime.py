"""Restricted process execution.

The engine only runs *known* executables with argument lists built in code.
There is intentionally no function that accepts a command string, and
``shell=True`` is never used.
"""

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

# Executables the engine is allowed to run, by logical name.
ALLOWED_TOOLS = frozenset({"nvidia-smi", "system_profiler", "sysctl", "colmap", "blender", "lspci"})

# Well-known install locations that are not usually on PATH.
_EXTRA_LOCATIONS: dict[str, tuple[str, ...]] = {
    "blender": (
        "/Applications/Blender.app/Contents/MacOS/Blender",
        "C:/Program Files/Blender Foundation/Blender/blender.exe",
    ),
    "colmap": (
        "/Applications/COLMAP.app/Contents/MacOS/colmap",
        # Homebrew and the self-contained install from docs/LOCAL_GPU_SETUP.md. Apps
        # started from Finder do not see the shell's PATH.
        "/opt/homebrew/bin/colmap",
        "/usr/local/bin/colmap",
        "~/.local/share/ai3d-studio/tools/colmap/bin/colmap",
    ),
}


# Explicit locations configured by the application (e.g. AI3D_COLMAP_PATH).
_CONFIGURED: dict[str, Path] = {}


def configure_tool(name: str, path: Path | None) -> None:
    """Use ``path`` for an allow-listed tool instead of searching PATH."""
    if name not in ALLOWED_TOOLS:
        raise ValueError(f"tool {name!r} is not on the allow-list")
    if path is None:
        _CONFIGURED.pop(name, None)
    else:
        _CONFIGURED[name] = path


def find_tool(name: str) -> Path | None:
    if name not in ALLOWED_TOOLS:
        raise ValueError(f"tool {name!r} is not on the allow-list")
    configured = _CONFIGURED.get(name)
    if configured is not None:
        return configured if configured.is_file() else None
    found = shutil.which(name)
    if found:
        return Path(found)
    for candidate in _EXTRA_LOCATIONS.get(name, ()):
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return None


def run_tool(name: str, args: Sequence[str], *, timeout: float = 10.0) -> str | None:
    """Run an allow-listed tool and return stdout, or None if unavailable/failed."""
    executable = find_tool(name)
    if executable is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed executable + code-built args
            [str(executable), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = completed.stdout or completed.stderr
    return output if completed.returncode == 0 or output else None
