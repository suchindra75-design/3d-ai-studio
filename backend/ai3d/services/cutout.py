"""Cut-out preview: what local single-image generation will see, before generating.

Runs the same background removal as local generation (the local AI engine's U²-Net,
or the photo's own transparency) on the same metadata-free copy generation uses,
then checks the result (``ai3d.processing.cutout``). The assessment is kept in the
photo's ``analysis["cutout"]`` and the picture the AI would receive is stored next
to the photo, so both go when the photo is deleted. Nothing leaves this computer.
"""

import io
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image as PILImage
from sqlalchemy.orm import Session

from ai3d.core.config import Settings
from ai3d.core.errors import AppError, InvalidInputError
from ai3d.core.timeutil import utcnow
from ai3d.models import Image
from ai3d.models.enums import InputMode
from ai3d.processing import cutout
from ai3d.providers.local_ai import LocalAIProvider
from ai3d.services.images import ensure_provider_copy, list_images
from ai3d.services.projects import get_project
from ai3d.storage import StorageBackend
from ai3d.storage.keys import image_cutout_key
from ai3d_engine.local_ai import engine as local_ai_engine

log = logging.getLogger(__name__)

# Bump when the checks or the preview change, so older results are re-run.
CUTOUT_VERSION = 1


def _own_transparency(path: Path) -> np.ndarray | None:
    """The photo's alpha when local generation would use it instead of U²-Net."""
    with PILImage.open(path) as opened:
        opened.load()
        if opened.mode != "RGBA":
            return None
        alpha = np.asarray(opened.getchannel("A"))
        return alpha if alpha.min() < 250 else None  # the engine's own rule (cut_out)


def engine_problem() -> str | None:
    """Why photos without transparency cannot be cut out here, or None."""
    return local_ai_engine.check_local_ai()


def has_result(image: Image) -> bool:
    found = image.analysis.get("cutout")
    return isinstance(found, dict) and found.get("version") == CUTOUT_VERSION


def can_check(image: Image) -> bool:
    """A photo with its own transparency needs no engine; others need the engine."""
    return image.analysis.get("subject_coverage") is not None or engine_problem() is None


def check_image(
    db: Session, storage: StorageBackend, settings: Settings, image: Image
) -> dict[str, Any]:
    """Make and assess the cut-out for one photo; returns the stored assessment."""
    copy = ensure_provider_copy(
        db, storage, settings, image, max_bytes=LocalAIProvider.capabilities().max_input_bytes
    )
    tmp_root = settings.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="cutout-", dir=tmp_root))
    try:
        photo = workdir / f"photo.{copy.storage_key.rsplit('.', 1)[-1]}"
        with storage.open(copy.storage_key) as src, photo.open("wb") as out:
            shutil.copyfileobj(src, out)
        alpha = _own_transparency(photo)
        removed = alpha is None
        if alpha is None:
            try:
                result = local_ai_engine.cut_out(photo, workdir / "engine")
            except local_ai_engine.LocalAINotInstalledError as exc:
                raise InvalidInputError(
                    f"The cut-out preview needs the local AI engine. {exc}",
                    code="local_ai_not_installed",
                ) from exc
            except local_ai_engine.LocalAIFailedError as exc:
                raise AppError(
                    f"The cut-out preview failed: {exc.message}",
                    code="cutout_failed",
                    status_code=500,
                ) from exc
            with PILImage.open(result["mask"]) as mask:
                alpha = np.asarray(mask.convert("L"))
        with PILImage.open(photo) as opened:
            rgb = np.asarray(opened.convert("RGB"))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    assessment = cutout.assess(rgb, alpha, background_removed=removed)
    buffer = io.BytesIO()
    cutout.ai_view(rgb, alpha).save(buffer, format="PNG", optimize=True)
    storage.save_bytes(image_cutout_key(image.project_id, image.id), buffer.getvalue())
    stored = assessment.to_dict() | {
        "version": CUTOUT_VERSION,
        "source_sha256": copy.sha256,
        "checked_at": utcnow().isoformat(),
    }
    image.analysis = {**image.analysis, "cutout": stored}
    db.commit()
    return stored


def check_project(
    db: Session, storage: StorageBackend, settings: Settings, project_id: Any
) -> list[Image]:
    """Cut out every photo of a single-image project that has no current result."""
    project = get_project(db, project_id)
    if project.input_mode != InputMode.SINGLE_IMAGE:
        raise InvalidInputError("The cut-out preview is for single-image projects.")
    images = list_images(db, project_id)
    if not images:
        raise InvalidInputError("Upload a photo first.")
    pending = [i for i in images if not has_result(i)]
    if not pending:
        return images
    runnable = [i for i in pending if can_check(i)]
    if not runnable:
        raise InvalidInputError(
            f"The cut-out preview needs the local AI engine. {engine_problem()}",
            code="local_ai_not_installed",
        )
    for image in runnable:
        check_image(db, storage, settings, image)
    return images
