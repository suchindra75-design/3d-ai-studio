"""Capture assistant: set-level assessment of a project's photos.

Per-image signals come from upload-time analysis. This module aggregates them
into checks that matter for reconstruction (coverage, blur, lighting and
camera consistency). Overlap needs feature matching, which runs on request
(``measure_overlap``) and is cached for the exact photo set; until then it is
reported as ``not_assessed`` rather than guessed.
"""

import hashlib
import json
import shutil
import statistics
import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ai3d.core.config import Settings
from ai3d.core.errors import InvalidInputError
from ai3d.core.timeutil import utcnow
from ai3d.models import Image
from ai3d.models.enums import InputMode, QualityLevel
from ai3d.processing.overlap import OverlapResult, assess_overlap
from ai3d.schemas.capture import CaptureAssessment, CaptureCheck
from ai3d.services.images import list_images
from ai3d.services.projects import get_project
from ai3d.storage import StorageBackend
from ai3d.storage.keys import overlap_analysis_key

# AI multi-view providers accept 2–4 views; photogrammetry needs many more.
MULTI_VIEW_MIN = 2
PHOTOGRAMMETRY_MIN = 20
PHOTOGRAMMETRY_RECOMMENDED = 40
LIGHTING_SPREAD_WARN = 0.25  # max-min mean brightness across the set
PROBLEM_SHARE_WARN = 0.3


def _warning_codes(image: Image) -> set[str]:
    warnings: list[dict[str, Any]] = image.analysis.get("warnings", [])
    return {str(w.get("code")) for w in warnings}


def _with_code(images: list[Image], *codes: str) -> list[Image]:
    return [i for i in images if _warning_codes(i) & set(codes)]


def _single_image_checks(images: list[Image]) -> list[CaptureCheck]:
    if not images:
        return [
            CaptureCheck(
                code="image_count",
                status="fail",
                title="No photo yet",
                message="Upload one clear photo of the object.",
            )
        ]
    checks = [
        CaptureCheck(
            code="image_count",
            status="pass" if len(images) == 1 else "warning",
            title="Photo uploaded" if len(images) == 1 else f"{len(images)} photos uploaded",
            message="Single-image generation uses one photo at a time; you choose which one "
            "when you start a generation."
            if len(images) > 1
            else "One photo is ready for single-image generation.",
        )
    ]
    poor = [i for i in images if i.quality_level == QualityLevel.POOR]
    best = max(images, key=lambda i: (i.quality_level != QualityLevel.POOR, i.sharpness or 0))
    checks.append(
        CaptureCheck(
            code="image_quality",
            status="fail"
            if best.quality_level == QualityLevel.POOR
            else ("warning" if best.quality_level == QualityLevel.WARNING else "pass"),
            title="Photo quality",
            message="The best photo has quality problems; see its warnings."
            if best.quality_level != QualityLevel.GOOD
            else "Sharp, well-exposed photo.",
            image_ids=[i.id for i in poor],
        )
    )
    checks.append(
        CaptureCheck(
            code="unseen_surfaces",
            status="warning",
            title="Hidden sides will be invented",
            message="A single photo cannot show the back or underside of the object; the AI "
            "model will infer those surfaces. Use a multi-view project for "
            "measured geometry.",
        )
    )
    return checks


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _count_check(n: int) -> CaptureCheck:
    if n < MULTI_VIEW_MIN:
        return CaptureCheck(
            code="image_count",
            status="fail",
            title="Not enough photos",
            message=f"Upload at least {MULTI_VIEW_MIN} photos from different angles.",
        )
    if n < PHOTOGRAMMETRY_MIN:
        return CaptureCheck(
            code="image_count",
            status="warning",
            title=f"{n} photos — enough for AI multi-view only",
            message=f"AI multi-view generation can use up to 4 of these views. Photogrammetry "
            f"needs at least {PHOTOGRAMMETRY_MIN} overlapping photos "
            f"({PHOTOGRAMMETRY_RECOMMENDED}+ recommended).",
        )
    if n < PHOTOGRAMMETRY_RECOMMENDED:
        return CaptureCheck(
            code="image_count",
            status="warning",
            title=f"{n} photos",
            message=f"Photogrammetry can run; {PHOTOGRAMMETRY_RECOMMENDED}+ photos usually "
            "give more complete surfaces.",
        )
    return CaptureCheck(
        code="image_count",
        status="pass",
        title=f"{n} photos",
        message="Good coverage for photogrammetry.",
    )


def _sharpness_check(images: list[Image]) -> CaptureCheck:
    blurry = _with_code(images, "blurry")
    soft = _with_code(images, "soft_focus")
    if blurry:
        return CaptureCheck(
            code="sharpness",
            status="fail" if len(blurry) / len(images) > PROBLEM_SHARE_WARN else "warning",
            title=_plural(len(blurry), "blurry photo"),
            message="Blurry photos produce false feature matches. Remove or retake them.",
            image_ids=[i.id for i in blurry],
        )
    if soft:
        return CaptureCheck(
            code="sharpness",
            status="warning",
            title=_plural(len(soft), "slightly soft photo"),
            message="Sharper photos give finer geometry.",
            image_ids=[i.id for i in soft],
        )
    return CaptureCheck(
        code="sharpness",
        status="pass",
        title="Photos are sharp",
        message="No blurry photos detected.",
    )


def _lighting_check(images: list[Image]) -> CaptureCheck:
    brightness = [float(i.analysis.get("brightness", 0.0)) for i in images]
    if max(brightness) - min(brightness) <= LIGHTING_SPREAD_WARN:
        return CaptureCheck(
            code="lighting_consistency",
            status="pass",
            title="Consistent lighting",
            message="Exposure is consistent across photos.",
        )
    mean = statistics.fmean(brightness)
    outliers = [
        i
        for i, b in zip(images, brightness, strict=True)
        if abs(b - mean) > LIGHTING_SPREAD_WARN / 2
    ]
    return CaptureCheck(
        code="lighting_consistency",
        status="warning",
        title="Lighting changes between photos",
        message="Brightness varies a lot across the set. Keep lighting and exposure "
        "fixed; changing light causes texture seams and matching errors.",
        image_ids=[i.id for i in outliers],
    )


def _camera_checks(images: list[Image]) -> list[CaptureCheck]:
    checks: list[CaptureCheck] = []
    if len({tuple(sorted((i.width, i.height))) for i in images}) > 1:
        checks.append(
            CaptureCheck(
                code="camera_consistency",
                status="warning",
                title="Mixed image resolutions",
                message="Photos have different resolutions. Use one camera with fixed settings.",
            )
        )

    focals = {round(i.focal_length_mm * 2) / 2 for i in images if i.focal_length_mm}
    if not focals:
        checks.append(
            CaptureCheck(
                code="focal_length",
                status="not_assessed",
                title="No focal-length metadata",
                message="The photos carry no EXIF focal length; camera intrinsics will be "
                "estimated during reconstruction.",
            )
        )
    elif len(focals) > 1:
        checks.append(
            CaptureCheck(
                code="focal_length",
                status="warning",
                title="Zoom changed between photos",
                message="Keep a fixed focal length (don't zoom) for the whole capture.",
            )
        )
    else:
        checks.append(
            CaptureCheck(
                code="focal_length",
                status="pass",
                title="Fixed focal length",
                message="All photos use the same focal length.",
            )
        )
    return checks


def _surface_checks(images: list[Image]) -> list[CaptureCheck]:
    checks: list[CaptureCheck] = []
    specular = _with_code(images, "specular_highlights")
    if len(specular) / len(images) > PROBLEM_SHARE_WARN:
        checks.append(
            CaptureCheck(
                code="reflective_surfaces",
                status="warning",
                title="Possible reflective surfaces",
                message="Many photos have blown-out highlights. Shiny, reflective or transparent "
                "objects reconstruct poorly; use diffuse light or matte scanning spray.",
                image_ids=[i.id for i in specular],
            )
        )
    low_texture = _with_code(images, "low_texture")
    if len(low_texture) / len(images) > PROBLEM_SHARE_WARN:
        checks.append(
            CaptureCheck(
                code="low_texture",
                status="warning",
                title="Low surface texture",
                message="Plain surfaces give few features to match. Add texture to the "
                "background or scene (e.g. a patterned mat).",
                image_ids=[i.id for i in low_texture],
            )
        )
    return checks


def _photo_set_signature(images: list[Image]) -> str:
    return hashlib.sha256("\n".join(sorted(i.sha256 for i in images)).encode()).hexdigest()


def _names(images: list[Image], ids: list[str], limit: int = 4) -> str:
    by_id = {str(i.id): i.original_filename for i in images}
    names = [by_id.get(i, "?") for i in ids]
    return ", ".join(names[:limit]) + ("…" if len(names) > limit else "")


def _overlap_check(images: list[Image], cached: OverlapResult | None) -> CaptureCheck:
    if len(images) < 2:
        return CaptureCheck(
            code="overlap",
            status="not_assessed",
            title="Overlap needs at least two photos",
            message="Aim for 60–80% overlap between consecutive shots.",
        )
    if cached is None:
        return CaptureCheck(
            code="overlap",
            status="not_assessed",
            title="Overlap not measured yet",
            message="Measure how well the photos overlap by matching features between them "
            "(takes a few seconds per 10 photos). Aim for 60–80% overlap between consecutive "
            "shots.",
            action="measure_overlap",
        )
    isolated = cached.isolated
    groups = [g for g in cached.groups if len(g) > 1]
    sampled = "" if cached.exhaustive else " (neighbouring photos and a sample were compared)"
    if not groups or len(groups[0]) < 3:
        return CaptureCheck(
            code="overlap",
            status="fail",
            title="Photos do not overlap",
            message="Almost no photo shares enough features with another, so photogrammetry "
            "cannot connect them. Take photos closer together (60–80% overlap) of a "
            "textured object.",
            image_ids=[uuid.UUID(k) for k in isolated],
            action="measure_overlap",
        )
    problems: list[str] = []
    if isolated:
        problems.append(
            f"{len(isolated)} photo(s) overlap with no other photo and will probably not be "
            f"used: {_names(images, isolated)}."
        )
    if len(groups) > 1:
        problems.append(
            f"The photos form {len(groups)} separate groups that do not connect "
            f"(largest: {len(groups[0])} photos). Add photos between them."
        )
    if problems:
        return CaptureCheck(
            code="overlap",
            status="warning",
            title="Some photos do not connect",
            message=" ".join(problems) + sampled,
            image_ids=[uuid.UUID(k) for k in isolated],
            action="measure_overlap",
        )
    median = statistics.median(cached.partners.values())
    return CaptureCheck(
        code="overlap",
        status="pass",
        title="Photos overlap well",
        message=f"All {len(cached.keys)} photos connect into one set; each overlaps with "
        f"{median:g} others on average (median){sampled}.",
    )


def _cached_overlap(
    storage: StorageBackend | None, project_id: uuid.UUID, images: list[Image]
) -> OverlapResult | None:
    if storage is None:
        return None
    key = overlap_analysis_key(project_id)
    if not storage.exists(key):
        return None
    with storage.open(key) as handle:
        data = json.load(handle)
    if data.get("signature") != _photo_set_signature(images):
        return None  # the photos changed since it was measured
    return OverlapResult.from_dict(data["result"])


def measure_overlap(
    db: Session, storage: StorageBackend, settings: Settings, project_id: uuid.UUID
) -> CaptureAssessment:
    """Match features between the project's photos and cache the result."""
    get_project(db, project_id)
    images = list_images(db, project_id)
    if len(images) < 2:
        raise InvalidInputError("Upload at least two photos to measure overlap.")
    tmp_root = settings.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="overlap-", dir=tmp_root))
    try:
        photos: list[tuple[str, Path]] = []
        for image in images:
            path = storage.local_path(image.storage_key)
            if path is None:
                path = workdir / str(image.id)
                with storage.open(image.storage_key) as src, path.open("wb") as out:
                    shutil.copyfileobj(src, out)
            photos.append((str(image.id), path))
        result = assess_overlap(photos)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    payload = {
        "signature": _photo_set_signature(images),
        "measured_at": utcnow().isoformat(),
        "result": result.to_dict(),
    }
    storage.save_bytes(overlap_analysis_key(project_id), json.dumps(payload).encode())
    return assess_capture(db, project_id, storage)


def _multi_view_checks(
    images: list[Image], overlap: OverlapResult | None = None
) -> list[CaptureCheck]:
    checks = [_count_check(len(images))]
    if not images:
        return checks
    checks.append(_sharpness_check(images))
    if len(images) >= 2:
        checks.append(_lighting_check(images))
    checks.extend(_camera_checks(images))
    checks.extend(_surface_checks(images))
    checks.append(_overlap_check(images, overlap))
    return checks


def assess_capture(
    db: Session, project_id: uuid.UUID, storage: StorageBackend | None = None
) -> CaptureAssessment:
    project = get_project(db, project_id)
    images = list_images(db, project_id)
    if project.input_mode == InputMode.SINGLE_IMAGE:
        checks = _single_image_checks(images)
        min_images, recommended = 1, 1
    else:
        checks = _multi_view_checks(images, _cached_overlap(storage, project_id, images))
        min_images, recommended = MULTI_VIEW_MIN, PHOTOGRAMMETRY_RECOMMENDED

    count_check = next(c for c in checks if c.code == "image_count")
    if count_check.status == "fail":
        readiness = "insufficient"
        summary = count_check.message
    elif any(c.status in {"fail", "warning"} for c in checks if c.code != "unseen_surfaces"):
        readiness = "needs_attention"
        summary = "Photos can be used, but some checks need attention."
    else:
        readiness = "ready"
        summary = "Photos look ready."

    return CaptureAssessment(
        input_mode=project.input_mode,
        image_count=len(images),
        min_images=min_images,
        recommended_images=recommended,
        readiness=readiness,
        summary=summary,
        checks=checks,
    )
