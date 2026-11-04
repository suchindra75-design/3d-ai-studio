"""Running OpenMVS safely: dense reconstruction, meshing and texturing on the CPU.

OpenMVS (AGPL-3.0) is an optional, separately installed program; AI 3D Studio
only starts its executables, it never links or bundles them. Only the programs
in :data:`PROGRAMS` can run, every argument list is built in this module from
typed values, ``shell=True`` is never used, and a run can be cancelled or time
out. Verified with OpenMVS 2.4.0 (conda-forge, CPU build) on a real
67-photo capture: dense points 71 s, mesh 41 s, texture 153 s on a 6-core
Intel Mac.
"""

import logging
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

PROGRAMS = ("InterfaceCOLMAP", "DensifyPointCloud", "ReconstructMesh", "TextureMesh")
TAIL_LINES = 40

# Where OpenMVS usually ends up when it is not on PATH (apps started from Finder do
# not see the shell's PATH). The first is the install from docs/LOCAL_GPU_SETUP.md.
_LOCATIONS = (
    "~/.local/share/ai3d-studio/tools/openmvs/bin",
    "/usr/local/bin/OpenMVS",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)

_configured: Path | None = None

CancelCheck = Callable[[], bool]


def configure(path: Path | None) -> None:
    """Use the OpenMVS programs in ``path`` (AI3D_OPENMVS_DIR) instead of searching."""
    global _configured  # noqa: PLW0603 - process-wide setting, like configure_tool
    _configured = path.expanduser() if path is not None else None


def _complete(folder: Path) -> bool:
    return all((folder / name).is_file() for name in PROGRAMS)


def find_openmvs() -> Path | None:
    """The folder holding every required OpenMVS program, or None."""
    if _configured is not None:
        return _configured if _complete(_configured) else None
    on_path = shutil.which(PROGRAMS[1])
    candidates = [Path(on_path).parent] if on_path else []
    candidates += [Path(p).expanduser() for p in _LOCATIONS]
    return next((c for c in candidates if _complete(c)), None)


def check_openmvs() -> str | None:
    """Why OpenMVS cannot be used, or None when it can."""
    if find_openmvs() is not None:
        return None
    where = f" in {_configured}" if _configured is not None else ""
    return (
        f"OpenMVS was not found{where}. Without it (or an NVIDIA GPU) photogrammetry builds the "
        "surface from sparse points, which is coarse on real photos. See docs/LOCAL_GPU_SETUP.md."
    )


class OpenMVSNotFoundError(RuntimeError):
    pass


class OpenMVSCancelledError(RuntimeError):
    pass


class OpenMVSFailedError(RuntimeError):
    def __init__(self, program: str, message: str, tail: list[str]) -> None:
        super().__init__(f"{program}: {message}")
        self.program = program
        self.message = message
        self.tail = tail


def _number(value: float, name: str) -> str:
    if not isinstance(value, int | float) or value != value or value < 0:  # noqa: PLR0124
        raise ValueError(f"invalid value for {name}: {value!r}")
    return str(value)


class OpenMVSRunner:
    """Runs the steps of one reconstruction in ``workspace``."""

    def __init__(self, workspace: Path, *, log_path: Path) -> None:
        folder = find_openmvs()
        if folder is None:
            raise OpenMVSNotFoundError(check_openmvs() or "OpenMVS was not found.")
        self.folder = folder
        self.workspace = workspace.resolve()
        self.log_path = log_path

    # -- the steps, with arguments built from typed values --------------------------------

    def import_colmap(
        self, dense_dir: Path, *, cancelled: CancelCheck, timeout: float | None
    ) -> Path:
        """COLMAP's undistorted workspace (image_undistorter) → ``scene.mvs``."""
        scene = self.workspace / "scene.mvs"
        dense = dense_dir.resolve()
        args = ["-i", str(dense), "-o", str(scene), "--image-folder", str(dense / "images")]
        self._run("InterfaceCOLMAP", args, cancelled=cancelled, timeout=timeout)
        return self._expect(scene, "InterfaceCOLMAP")

    def densify(
        self, scene: Path, *, resolution_level: int, cancelled: CancelCheck, timeout: float | None
    ) -> Path:
        """Depth maps for every photo, fused into a dense point cloud (``scene_dense.mvs``)."""
        if resolution_level not in {0, 1, 2, 3}:
            raise ValueError("resolution_level must be 0–3")
        args = [str(scene), "--resolution-level", str(resolution_level)]
        self._run("DensifyPointCloud", args, cancelled=cancelled, timeout=timeout)
        return self._expect(self.workspace / "scene_dense.mvs", "DensifyPointCloud")

    def mesh(
        self, dense_scene: Path, *, decimate: float, cancelled: CancelCheck, timeout: float | None
    ) -> Path:
        """A watertight-ish surface through the dense points (``scene_dense_mesh.ply``)."""
        if not 0 < decimate <= 1:
            raise ValueError("decimate must be in (0, 1]")
        out = self.workspace / "scene_dense_mesh.ply"
        args = [str(dense_scene), "-o", str(out), "--decimate", _number(decimate, "decimate")]
        self._run("ReconstructMesh", args, cancelled=cancelled, timeout=timeout)
        return self._expect(out, "ReconstructMesh")

    def texture(
        self, dense_scene: Path, mesh: Path, *, cancelled: CancelCheck, timeout: float | None
    ) -> Path:
        """Texture atlas from the photos; an OBJ with its MTL and images."""
        out = self.workspace / "scene_textured.obj"
        args = [str(dense_scene), "-m", str(mesh), "-o", str(out), "--export-type", "obj"]
        self._run("TextureMesh", args, cancelled=cancelled, timeout=timeout)
        return self._expect(out, "TextureMesh")

    # -- process handling ---------------------------------------------------------------

    def _expect(self, path: Path, program: str) -> Path:
        if not path.is_file() or path.stat().st_size == 0:
            raise OpenMVSFailedError(program, f"did not write {path.name}", [])
        return path

    def _run(
        self, program: str, args: list[str], *, cancelled: CancelCheck, timeout: float | None
    ) -> None:
        if program not in PROGRAMS:
            raise ValueError(f"OpenMVS program {program!r} is not allowed")
        if any("\x00" in a or "\n" in a for a in args):
            raise ValueError("invalid OpenMVS argument")
        command = [str(self.folder / program), "-w", str(self.workspace), *args]
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        started = time.monotonic()
        with self.log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"$ {program} {' '.join(command[1:])}\n")
            log_file.flush()
            process = subprocess.Popen(  # noqa: S603 - allow-listed program, code-built args
                command,
                cwd=self.workspace,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                shell=False,
            )

            def pump() -> None:
                assert process.stdout is not None
                for line in process.stdout:
                    log_file.write(line)
                    tail.append(line.rstrip())

            reader = threading.Thread(target=pump, name=f"openmvs-{program}", daemon=True)
            reader.start()
            try:
                while process.poll() is None:
                    if cancelled():
                        raise OpenMVSCancelledError(program)
                    if timeout is not None and time.monotonic() - started > timeout:
                        raise OpenMVSFailedError(
                            program, f"did not finish within {timeout / 60:g} minutes", list(tail)
                        )
                    time.sleep(0.25)
            finally:
                if process.poll() is None:
                    _stop(process)
                reader.join(timeout=2)
        if process.returncode != 0:
            raise OpenMVSFailedError(program, f"exited with code {process.returncode}", list(tail))
        log.info(
            "openmvs step finished",
            extra={"program": program, "seconds": time.monotonic() - started},
        )


def _stop(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
