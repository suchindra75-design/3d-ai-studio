"""The refining step of a generation job, shared by the local AI worker and the
GPU server's results (``processing/refine.py`` does the work)."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from PIL import Image

from ai3d.models.enums import JobEventType
from ai3d.processing.refine import RefineReport, RefineSettings, ViewPhoto, refine
from ai3d.workers.runner import JobContext

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RefineInputs:
    """The framed photos that came with a model."""

    source: str  # who made the model, for messages ("TripoSR", "TRELLIS")
    photo: Path | None = None  # the front photo, framed as the model saw it (RGBA)
    photo_crop_side: float | None = None  # its square's side in photo pixels
    views: Sequence[tuple[str, Path, float | None]] = field(default_factory=list)
    orient: bool = False  # turn the model to face the front photo first
    correct_depth: bool = True  # let side photos correct the depth


def _keep_alive(ctx: JobContext) -> None:
    ctx.heartbeat()  # raises when the runner stops or the lease is lost


def _rgba(path: Path) -> np.ndarray:
    with Image.open(path) as opened:
        return np.asarray(opened.convert("RGBA"))


def views_part(views: list[dict[str, Any]], depth: dict[str, Any] | None) -> str:
    """What the other views did, for the job log ("" without other views)."""
    if not views and not depth:
        return ""
    parts = []
    if depth and depth.get("applied"):
        parts.append(
            f"The side photos corrected the depth ({depth['depth_before']:.2f} → "
            f"{depth['depth_after']:.2f} model units front to back)"
        )
    elif depth:
        notes = [v.get("note") for v in depth.get("views", []) if v.get("note")]
        reason = notes[0] if notes else depth.get("note", "")
        parts.append(f"The depth was not corrected: {reason}".rstrip(": "))
    used = [v["role"] for v in views if v.get("used")]
    unused = [v["role"] for v in views if not v.get("used")]
    if used:
        plural = "s" if len(used) > 1 else ""
        parts.append(f"the {', '.join(used)} photo{plural} coloured those sides")
    if unused:
        plural, verb = ("s", "were") if len(unused) > 1 else ("", "was")
        parts.append(f"the {', '.join(unused)} photo{plural} did not line up and {verb} not used")
    return " " + "; ".join(parts) + "."


def _message(report: RefineReport) -> str:
    pieces = report.fragments_removed
    removed = f", {pieces} loose fragment{'s' if pieces != 1 else ''} removed" if pieces else ""
    if report.photo_projected:
        photo_part = (
            f"the photo was projected onto {report.photo_share:.0%} of the surface "
            f"(outline overlap {report.photo_fit_iou:.0%})"
        )
    else:
        photo_part = report.note or "no photo was available to project"
    turned = ""
    if report.orientation:
        turned = f" Turned {report.orientation['yaw_degrees']:.0f}° to face the front photo."
    size = report.texture_size
    return (
        f"Refined in {sum(report.seconds.values()):.0f} s: {report.triangles_before:,} → "
        f"{report.triangles_after:,} triangles{removed}; colours baked into a "
        f"{size}×{size} px texture, and {photo_part}.{turned}"
        f"{views_part(report.views, report.depth)}"
    )


def refine_model(
    ctx: JobContext,
    glb: Path,
    options: Any,
    inputs: RefineInputs,
    *,
    progress: float,
) -> tuple[Path, dict[str, Any] | None]:
    """The refined model's path and report; the model as made if refining is not
    planned (jobs planned before it existed) or fails (the job says so)."""
    if not isinstance(options, dict):
        return glb, None
    ctx.stage("refining", "Cleaning up the surface and baking its texture.", progress)
    try:
        mesh = trimesh.load(glb, force="mesh")
        if not isinstance(mesh, trimesh.Trimesh):
            raise TypeError(f"{inputs.source}'s file did not open as one mesh")
        photo = _rgba(inputs.photo) if inputs.photo and inputs.photo.is_file() else None
        views = [
            ViewPhoto(role, _rgba(path), float(side or 0.0))
            for role, path, side in inputs.views
            if path.is_file()
        ]
        refined, report = refine(
            mesh,
            RefineSettings(
                triangles=int(options["triangles"]), texture_size=int(options["texture_size"])
            ),
            photo,
            on_step=lambda _: _keep_alive(ctx),
            views=views,
            photo_crop_side=inputs.photo_crop_side if inputs.correct_depth else None,
            orient=inputs.orient,
        )
        output = glb.with_name("refined.glb")
        refined.export(output, file_type="glb", include_normals=True)
    except Exception as exc:  # the unrefined model is still a usable result
        log.warning("refining failed", exc_info=True, extra={"job_id": str(ctx.job_id)})
        ctx.event(
            JobEventType.LOG,
            f"Refining the model did not work ({type(exc).__name__}: {exc}); "
            f"it is kept as {inputs.source} made it.",
            stage="refining",
        )
        return glb, {"error": f"{type(exc).__name__}: {exc}"}
    details = report.to_dict()
    ctx.event(JobEventType.LOG, _message(report), stage="refining", data={"refine": details})
    return output, details
