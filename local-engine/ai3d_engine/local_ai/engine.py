"""Running the local AI engine (TripoSR) safely.

The engine lives in its own folder with its own Python environment (PyTorch
needs versions the backend cannot share). It is only ever started as::

    <engine>/env/bin/python -I <this package's local_ai_scripts/triposr_generate.py>
        <request.json> <result.json>

with a request written here from typed values. No shell, no user-provided
code, a timeout, and cancellation that kills the process.
"""

import json
import logging
import os
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai3d_engine.local_ai import manifest

log = logging.getLogger(__name__)

SCRIPT = Path(__file__).resolve().parent.parent / "local_ai_scripts" / "triposr_generate.py"
TAIL_LINES = 30
STAGES = ("starting", "background", "loading", "generating", "meshing")

CancelCheck = Callable[[], bool]
ProgressCallback = Callable[[str, float], None]

_configured: Path | None = None


def default_dir() -> Path:
    return Path.home() / ".local" / "share" / "ai3d-studio" / "tools" / "local-ai"


def configure(path: Path | None) -> None:
    """Use ``path`` as the engine folder instead of the default (AI3D_LOCAL_AI_DIR)."""
    global _configured  # noqa: PLW0603 - process-wide setting, like configure_tool
    _configured = path.expanduser() if path is not None else None


def engine_dir() -> Path:
    return _configured or default_dir()


def env_python(root: Path) -> Path:
    return root / "env" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


@dataclass(frozen=True, slots=True)
class EngineStatus:
    installed: bool
    path: Path
    problem: str | None
    info: dict[str, Any]


def status(root: Path | None = None) -> EngineStatus:
    """Whether the engine is installed and complete (cheap: no hashing)."""
    root = root or engine_dir()
    record_path = root / "engine.json"
    if not record_path.is_file():
        return EngineStatus(
            False,
            root,
            "The local AI engine (TripoSR) is not installed. Install it with "
            "`uv run ai3d-local-ai install` (about 3 GB; see docs/LOCAL_GPU_SETUP.md).",
            {},
        )
    try:
        info = json.loads(record_path.read_text(encoding="utf-8"))
    except ValueError:
        info = {}
    if info.get("manifest_version") != manifest.MANIFEST_VERSION:
        return EngineStatus(
            False, root, "The local AI engine is from another version; reinstall it.", info
        )
    missing = [
        a.path
        for a in manifest.WEIGHTS
        if not (root / a.path).is_file() or (root / a.path).stat().st_size != a.size
    ]
    if not env_python(root).is_file() or not (root / info.get("source_dir", "-")).is_dir():
        missing.append("env or source")
    if missing:
        return EngineStatus(
            False, root, f"The local AI engine is incomplete ({', '.join(missing)}).", info
        )
    return EngineStatus(True, root, None, info)


def check_local_ai() -> str | None:
    """Why the engine cannot be used, or None when it can."""
    return status().problem


class LocalAINotInstalledError(RuntimeError):
    pass


class LocalAICancelledError(RuntimeError):
    pass


class LocalAIFailedError(RuntimeError):
    def __init__(self, message: str, tail: list[str]) -> None:
        super().__init__(message)
        self.message = message
        self.tail = tail


def build_request(
    root: Path,
    info: dict[str, Any],
    *,
    image: Path,
    output: Path,
    prepared_image: Path,
    resolution: int,
    device: str = "auto",
    threads: int | None = None,
) -> dict[str, Any]:
    if not 32 <= resolution <= 512:
        raise ValueError("resolution must be between 32 and 512")
    if device not in {"auto", "cpu"}:
        raise ValueError("device must be 'auto' or 'cpu'")
    return {
        "source_dir": str(root / info["source_dir"]),
        "weights_dir": str(root / "models" / "triposr"),
        "dino_config": str(root / "models" / "dino-vitb16" / "config.json"),
        "u2net": str(root / "models" / "u2net.onnx"),
        "image": str(image),
        "output": str(output),
        "prepared_image": str(prepared_image),
        "resolution": int(resolution),
        "device": device,
        "threads": threads or physical_cores(),
    }


def physical_cores() -> int:
    try:
        import psutil  # noqa: PLC0415

        return int(psutil.cpu_count(logical=False) or os.cpu_count() or 1)
    except ImportError:  # pragma: no cover - psutil is a dependency of the engine package
        return os.cpu_count() or 1


def _child_env(root: Path) -> dict[str, str]:
    """A minimal environment: no proxies or tokens, and every model lookup offline."""
    keep = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "LANG", "LC_ALL")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HOME": str(root / "hf-cache"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def generate(
    image: Path,
    workdir: Path,
    *,
    resolution: int,
    timeout: float = 1800.0,
    cancel: CancelCheck | None = None,
    on_progress: ProgressCallback | None = None,
    device: str = "auto",
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    """Turn one photo into ``workdir/model.glb``. Returns the script's result.

    Raises LocalAINotInstalledError, LocalAICancelledError or LocalAIFailedError.
    """
    state = status()
    if not state.installed:
        raise LocalAINotInstalledError(state.problem or "not installed")
    workdir.mkdir(parents=True, exist_ok=True)
    request = build_request(
        state.path,
        state.info,
        image=image,
        output=workdir / "model.glb",
        prepared_image=workdir / "prepared.png",
        resolution=resolution,
        device=device,
    )
    request_path, result_path = workdir / "request.json", workdir / "result.json"
    log_path = workdir / "local-ai.log"
    result_path.unlink(missing_ok=True)
    request_path.write_text(json.dumps(request), encoding="utf-8")
    args = [str(env_python(state.path)), "-I", str(SCRIPT), str(request_path), str(result_path)]
    started = time.monotonic()
    read_at = 0
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        process = subprocess.Popen(  # noqa: S603 - fixed executable + code-built args
            args,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=workdir,
            env=_child_env(state.path),
            shell=False,
        )
        try:
            while True:
                try:
                    returncode = process.wait(timeout=poll_seconds)
                    break
                except subprocess.TimeoutExpired:
                    pass
                finally:
                    read_at = _report_progress(log_path, read_at, on_progress)
                if cancel is not None and cancel():
                    raise LocalAICancelledError
                if time.monotonic() - started > timeout:
                    raise LocalAIFailedError(
                        f"The local AI engine did not finish within {timeout / 60:g} minutes.", []
                    )
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
    if result and result.get("ok") is True and returncode == 0:
        result["seconds_wall"] = round(time.monotonic() - started, 2)
        result["glb"] = str(workdir / "model.glb")
        result["prepared_image"] = str(workdir / "prepared.png")
        return result
    message = (
        str(result["error"])
        if result and result.get("error")
        else f"The local AI engine stopped with code {returncode}."
    )
    log.info("local AI run failed", extra={"tail": tail[-5:]})
    raise LocalAIFailedError(message, tail)


def _report_progress(log_path: Path, offset: int, on_progress: ProgressCallback | None) -> int:
    """Reads new ``PROGRESS stage fraction`` lines from the log."""
    try:
        with log_path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
    except OSError:
        return offset
    end = chunk.rfind(b"\n") + 1
    for line in chunk[:end].decode("utf-8", "replace").splitlines():
        parts = line.split()
        if on_progress is not None and len(parts) == 3 and parts[0] == "PROGRESS":
            if parts[1] not in STAGES:
                continue
            try:
                fraction = float(parts[2])
            except ValueError:
                continue
            on_progress(parts[1], fraction)
    return offset + end
