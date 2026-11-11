"""The job queue: one GPU, one job at a time, results on disk until fetched or old.

Each job has its own folder: the uploaded photos, then ``model.glb`` (glTF axes,
linear vertex colours as glTF requires), ``cutout-<role>.png`` (each photo framed
as the model saw it, with transparency) and ``result.json``.
"""

import json
import logging
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import trimesh
from PIL import Image

from ai3d_gpu.backends.base import Backend, CancelledError, Generated, GenerationSettings, ViewPhoto
from ai3d_gpu.framing import NoObjectError

log = logging.getLogger(__name__)

Status = Literal["queued", "running", "succeeded", "failed", "cancelled"]
TERMINAL = {"succeeded", "failed", "cancelled"}


@dataclass(slots=True)
class Job:
    id: str
    folder: Path
    photos: list[tuple[str, Path]]  # (role, path)
    settings: GenerationSettings
    status: Status = "queued"
    stage: str | None = None
    progress: float = 0.0
    message: str = "Waiting for the GPU."
    error: str | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None
    cancel_requested: bool = False
    result: dict[str, Any] | None = None

    def read(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 3),
            "message": self.message,
            "error": self.error,
            "files": sorted(self.result["files"]) if self.result else [],
        }


class QueueFullError(Exception):
    pass


def _srgb_to_linear(srgb: np.ndarray) -> np.ndarray:
    srgb = np.clip(srgb, 0.0, 1.0)
    return np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)


def write_result(generated: Generated, folder: Path) -> dict[str, Any]:
    """Save the mesh and framed photos; returns what ``result.json`` holds."""
    mesh = trimesh.Trimesh(vertices=generated.vertices, faces=generated.faces, process=False)
    if generated.colours is not None:
        linear = _srgb_to_linear(np.asarray(generated.colours, dtype=np.float64))
        rgba = np.hstack([np.round(linear * 255), np.full((len(linear), 1), 255)])
        mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=rgba.astype(np.uint8))
    mesh.export(folder / "model.glb", file_type="glb", include_normals=True)
    files = ["model.glb"]
    views = []
    for role, framed in generated.framed.items():
        name = f"cutout-{role}.png"
        framed.cutout.save(folder / name)
        files.append(name)
        views.append({"role": role, "cutout": name, "crop_side": round(framed.side, 2)})
    result = {
        "files": files,
        "views": views,
        "vertices": len(mesh.vertices),
        "triangles": len(mesh.faces),
        "coloured": generated.colours is not None,
        **generated.info,
    }
    (folder / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return result


class JobQueue:
    def __init__(
        self, backend: Backend, work_dir: Path, *, max_queue: int, keep_seconds: int
    ) -> None:
        self.backend = backend
        self.work_dir = work_dir
        self.max_queue = max_queue
        self.keep_seconds = keep_seconds
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._stopping = False
        work_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, name="gpu-jobs", daemon=True)
        self._thread.start()

    # -- API ---------------------------------------------------------------------

    def submit(self, photos: list[tuple[str, bytes]], settings: GenerationSettings) -> Job:
        with self._lock:
            self._forget_old()
            waiting = sum(1 for job in self._jobs.values() if job.status in {"queued", "running"})
            if waiting >= self.max_queue:
                raise QueueFullError
            job_id = uuid.uuid4().hex
            folder = self.work_dir / job_id
            folder.mkdir(parents=True)
            stored = []
            for index, (role, data) in enumerate(photos):
                path = folder / f"input-{index}-{role}"
                path.write_bytes(data)
                stored.append((role, path))
            job = Job(id=job_id, folder=folder, photos=stored, settings=settings)
            self._jobs[job_id] = job
            self._wake.notify_all()
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status == "queued":
                self._finish(job, "cancelled", "Cancelled before it started.")
            elif job.status == "running":
                job.cancel_requested = True
                job.message = "Cancelling…"
            return job

    def delete(self, job_id: str) -> bool:
        """Forget a finished job and delete its files (the app does this after download)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in TERMINAL:
                return False
            del self._jobs[job_id]
        shutil.rmtree(job.folder, ignore_errors=True)
        return True

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "queued": sum(1 for j in self._jobs.values() if j.status == "queued"),
                "running": sum(1 for j in self._jobs.values() if j.status == "running"),
            }

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            self._wake.notify_all()
        self._thread.join(timeout=5)

    # -- worker ------------------------------------------------------------------

    def _finish(self, job: Job, status: Status, message: str, error: str | None = None) -> None:
        job.status = status
        job.message = message
        job.error = error
        job.finished = time.time()
        if status != "succeeded":
            for _, path in job.photos:
                path.unlink(missing_ok=True)

    def _forget_old(self) -> None:
        cutoff = time.time() - self.keep_seconds
        for job_id, job in list(self._jobs.items()):
            if job.finished is not None and job.finished < cutoff:
                del self._jobs[job_id]
                shutil.rmtree(job.folder, ignore_errors=True)

    def _next(self) -> Job | None:
        with self._lock:
            while not self._stopping:
                queued = [j for j in self._jobs.values() if j.status == "queued"]
                if queued:
                    job = min(queued, key=lambda j: j.created)
                    job.status = "running"
                    job.message = "Starting on the GPU."
                    return job
                self._wake.wait(timeout=60)
                self._forget_old()
            return None

    def _loop(self) -> None:
        while (job := self._next()) is not None:
            self._run(job)

    def _run(self, job: Job) -> None:
        def progress(stage: str, fraction: float) -> None:
            job.stage = stage
            job.progress = max(0.0, min(1.0, fraction))
            job.message = {
                "preparing": "Removing the background and framing the photos.",
                "generating": "Generating the 3D shape.",
                "meshing": "Building the surface.",
            }.get(stage, stage)

        started = time.perf_counter()
        try:
            views = []
            for role, path in job.photos:
                with Image.open(path) as opened:
                    opened.load()
                    views.append(ViewPhoto(role, opened.copy()))  # type: ignore[arg-type]
            generated = self.backend.generate(
                views, job.settings, progress, lambda: job.cancel_requested
            )
            result = write_result(generated, job.folder)
            result["seconds"] = round(time.perf_counter() - started, 1)
            result["model"] = self.backend.model
            result["device"] = self.backend.device
            (job.folder / "result.json").write_text(json.dumps(result), encoding="utf-8")
            with self._lock:
                job.result = result
                self._finish(job, "succeeded", f"Done in {result['seconds']:.0f} s.")
        except CancelledError:
            with self._lock:
                self._finish(job, "cancelled", "Cancelled.")
        except NoObjectError as exc:
            with self._lock:
                self._finish(job, "failed", str(exc), "no_object")
        except Exception as exc:
            log.error("job failed: %s", traceback.format_exc())
            with self._lock:
                self._finish(
                    job, "failed", f"The GPU job failed: {type(exc).__name__}: {exc}", "failed"
                )
