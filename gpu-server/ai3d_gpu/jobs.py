"""The job queue: one GPU, one job at a time, results on disk until fetched or old.

Each job has its own folder and a kind:

* ``model``: the uploaded photos, then ``model.glb`` (glTF axes, linear vertex
  colours as glTF requires) and ``cutout-<role>.png`` (each photo framed as the
  model saw it, with transparency);
* ``text``: ``text.json`` (``{"text": ...}``), the text model's reply;
* ``image``: ``image.png``.

Every finished job also has ``result.json``. Before a job runs, the models of the
other kinds are moved off the GPU (``park``), so one GPU can hold all three.
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

from ai3d_gpu.backends import Engines
from ai3d_gpu.backends.base import (
    CancelledError,
    Generated,
    GenerationSettings,
    ImageRequest,
    TextRequest,
    ViewPhoto,
)
from ai3d_gpu.framing import NoObjectError

log = logging.getLogger(__name__)

Status = Literal["queued", "running", "succeeded", "failed", "cancelled"]
Kind = Literal["model", "text", "image"]
TERMINAL = {"succeeded", "failed", "cancelled"}


@dataclass(slots=True)
class Job:
    id: str
    folder: Path
    kind: Kind = "model"
    photos: list[tuple[str, Path]] = field(default_factory=list)  # (role, path)
    settings: GenerationSettings = field(default_factory=GenerationSettings)
    request: TextRequest | ImageRequest | None = None
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
            "kind": self.kind,
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
        self, engines: Engines, work_dir: Path, *, max_queue: int, keep_seconds: int
    ) -> None:
        self.engines = engines
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

    def _new_job(self) -> tuple[str, Path]:
        """Call with the lock held."""
        self._forget_old()
        waiting = sum(1 for job in self._jobs.values() if job.status in {"queued", "running"})
        if waiting >= self.max_queue:
            raise QueueFullError
        job_id = uuid.uuid4().hex
        folder = self.work_dir / job_id
        folder.mkdir(parents=True)
        return job_id, folder

    def _enqueue(self, job: Job) -> Job:
        """Call with the lock held."""
        self._jobs[job.id] = job
        self._wake.notify_all()
        return job

    def submit(self, photos: list[tuple[str, bytes]], settings: GenerationSettings) -> Job:
        with self._lock:
            job_id, folder = self._new_job()
            stored = []
            for index, (role, data) in enumerate(photos):
                path = folder / f"input-{index}-{role}"
                path.write_bytes(data)
                stored.append((role, path))
            return self._enqueue(Job(id=job_id, folder=folder, photos=stored, settings=settings))

    def submit_request(self, kind: Kind, request: TextRequest | ImageRequest) -> Job:
        with self._lock:
            job_id, folder = self._new_job()
            return self._enqueue(Job(id=job_id, folder=folder, kind=kind, request=request))

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

    def _engine(self, kind: Kind) -> Any:
        engine = {
            "model": self.engines.shape,
            "text": self.engines.text,
            "image": self.engines.image,
        }[kind]
        if engine is None:
            raise RuntimeError(f"This server has no {kind} model.")
        return engine

    def _make_room(self, kind: Kind) -> None:
        """Move the other kinds' models off the GPU before this one runs."""
        using = self._engine(kind)
        for other in (self.engines.shape, self.engines.text, self.engines.image):
            if other is not None and other is not using:
                park = getattr(other, "park", None)
                if park is not None:
                    park()

    def _produce(self, job: Job, progress: Any) -> dict[str, Any]:
        """Run the job's model and write its files; returns what ``result.json`` holds."""

        def cancelled() -> bool:
            return job.cancel_requested

        if job.kind == "text":
            assert isinstance(job.request, TextRequest)
            progress("writing", 0.0)
            text = self.engines.text.write(job.request, cancelled)  # type: ignore[union-attr]
            (job.folder / "text.json").write_text(json.dumps({"text": text}), encoding="utf-8")
            return {"files": ["text.json"], "characters": len(text)}
        if job.kind == "image":
            assert isinstance(job.request, ImageRequest)
            image = self.engines.image.paint(job.request, progress, cancelled)  # type: ignore[union-attr]
            image.save(job.folder / "image.png")
            return {
                "files": ["image.png"],
                "width": image.width,
                "height": image.height,
                "seed": job.request.settings.seed,
            }
        views = []
        for role, path in job.photos:
            with Image.open(path) as opened:
                opened.load()
                views.append(ViewPhoto(role, opened.copy()))  # type: ignore[arg-type]
        generated = self.engines.shape.generate(views, job.settings, progress, cancelled)
        return write_result(generated, job.folder)

    def _run(self, job: Job) -> None:
        def progress(stage: str, fraction: float) -> None:
            job.stage = stage
            job.progress = max(0.0, min(1.0, fraction))
            job.message = {
                "preparing": "Removing the background and framing the photos.",
                "generating": "Generating the 3D shape.",
                "meshing": "Building the surface.",
                "writing": "Writing the scene plan.",
                "painting": "Painting the picture.",
            }.get(stage, stage)

        started = time.perf_counter()
        try:
            engine = self._engine(job.kind)
            self._make_room(job.kind)
            result = self._produce(job, progress)
            result["seconds"] = round(time.perf_counter() - started, 1)
            result["model"] = engine.model
            result["device"] = engine.device
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
