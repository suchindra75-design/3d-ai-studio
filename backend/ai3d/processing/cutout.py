"""What local single-image generation will see: the cut-out and how it is framed.

Local generation (TripoSR) first separates the object from its background with a
mask, then crops to the masked area and places it on plain grey. Problems there
are invisible until the model is made, so these pure functions (numpy/OpenCV, no
database or storage) check the mask itself:

- **Framing:** whether the object runs off an edge of the photo (that part will be
  cut flat), or is too small to keep detail.
- **Background match:** how many of the colours along the object's edge also occur in
  the background right beside it. Where object and background look alike, masks lose
  parts, thin ones first (and the lost parts never show in the outline itself).

Thresholds are heuristics; they flag likely problems, they do not prove them.
"""

from dataclasses import asdict, dataclass, field
from typing import Any

import cv2
import numpy as np
from PIL import Image
from scipy.spatial import cKDTree

from ai3d.processing.images import QualityWarning, frame_touches, join_sides

ANALYSIS_MAX_SIDE = 512
MASK_THRESHOLD = 127  # alpha above this counts as the object
NO_SUBJECT = 0.01  # the generator refuses masks smaller than this share of the photo
SMALL_SUBJECT = 0.04
FOREGROUND_RATIO = 0.85  # how much of the AI's square view the object fills (TripoSR default)
PREVIEW_SIZE = 512
# Background match: edge bands are this share of the long side wide; colours within
# MATCH_DELTA_E (CIELAB ΔE*76, about "just noticeable" ×2) count as the same. On test
# pictures cut out by the engine, plain contrasting backgrounds scored 0.01-0.44,
# white on white 0.75, dark grey on black 0.70-0.98, and a dark-suited figure on a
# black and red backdrop 0.89 (it lost its ears).
EDGE_BAND = 0.05
MATCH_DELTA_E = 5.0
MATCH_SAMPLES = 3000
MIN_BAND_PIXELS = 50
BACKGROUND_MATCH_WARN = 0.6


@dataclass(slots=True)
class CutoutAssessment:
    background_removed: bool  # False when the photo's own transparency was used
    coverage: float  # share of the photo inside the mask
    touches: list[str]  # frame edges the object runs off
    background_match: float | None  # see background_match(); None when not measured
    warnings: list[QualityWarning] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _downscaled(rgb: np.ndarray, alpha: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    height, width = alpha.shape
    scale = ANALYSIS_MAX_SIDE / max(height, width)
    if scale >= 1:
        return rgb, alpha
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return (
        cv2.resize(rgb, size, interpolation=cv2.INTER_AREA),
        cv2.resize(alpha, size, interpolation=cv2.INTER_AREA),
    )


def background_match(rgb: np.ndarray, mask: np.ndarray) -> float | None:
    """Share of the object's edge colours that also occur in the background beside it.

    Compares a band just inside the mask's outline with a band just outside it (each
    ``EDGE_BAND`` of the photo's long side wide; the photo's own border is left out). A
    colour counts as shared when the background band has one within ``MATCH_DELTA_E``.
    High values mean the object blends into its surroundings, which is where masks lose
    parts. None when there is no outline to compare (no object, or nothing else).
    """
    if mask.all() or not mask.any():
        return None
    radius = max(2, round(EDGE_BAND * max(mask.shape)))
    disk = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    solid = mask.astype(np.uint8)
    inner = mask & (cv2.erode(solid, disk, borderValue=1) == 0)
    outer = (cv2.dilate(solid, disk) > 0) & ~mask
    for band in (inner, outer):  # the frame's edge is not an outline
        band[:2, :] = band[-2:, :] = False
        band[:, :2] = band[:, -2:] = False
    if inner.sum() < MIN_BAND_PIXELS or outer.sum() < MIN_BAND_PIXELS:
        return None
    lab = cv2.cvtColor(rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)
    rng = np.random.default_rng(0)  # fixed, so the same photo always scores the same

    def sample(band: np.ndarray) -> np.ndarray:
        colours: np.ndarray = lab[band]
        if len(colours) > MATCH_SAMPLES:
            colours = colours[rng.choice(len(colours), MATCH_SAMPLES, replace=False)]
        return colours

    distance, _ = cKDTree(sample(outer)).query(sample(inner))
    return float((distance < MATCH_DELTA_E).mean())


def assess(rgb: np.ndarray, alpha: np.ndarray, *, background_removed: bool) -> CutoutAssessment:
    """Check a photo (H×W×3 uint8) and the mask generation would use (H×W uint8)."""
    rgb, alpha = _downscaled(rgb, alpha)
    mask = alpha > MASK_THRESHOLD
    coverage = float(mask.mean())
    warnings: list[QualityWarning] = []
    if coverage < NO_SUBJECT:
        warnings.append(
            QualityWarning(
                "no_subject",
                "error",
                "No object was found in the photo, so generation would fail. Photograph the "
                "object against a plain background, filling more of the frame.",
            )
        )
        return CutoutAssessment(background_removed, round(coverage, 4), [], None, warnings)

    touches = frame_touches(mask)
    if touches:
        warnings.append(
            QualityWarning(
                "subject_cropped",
                "warning",
                f"The object runs off the {join_sides(touches)} edge"
                f"{'s' if len(touches) > 1 else ''} of the photo. The AI can only model what "
                "is in the frame, so it will be cut flat there. Leave space around the whole "
                "object.",
            )
        )
    if coverage < SMALL_SUBJECT:
        warnings.append(
            QualityWarning(
                "subject_small",
                "warning",
                f"The object fills only {coverage:.0%} of the photo, so little of its detail "
                "reaches the AI. Move closer or crop the photo.",
            )
        )

    match = background_match(rgb, mask) if background_removed else None
    if match is not None and match >= BACKGROUND_MATCH_WARN:
        warnings.append(
            QualityWarning(
                "low_edge_contrast",
                "warning",
                f"{match:.0%} of the colours along the object's edge also appear in the "
                "background right next to it, so the cut-out can lose parts (thin ones such "
                "as ears, handles or legs first). Use a plain background that contrasts with "
                "the object, or upload a PNG with the background already removed.",
            )
        )
    if not background_removed:
        warnings.append(
            QualityWarning(
                "own_transparency",
                "info",
                "The photo's own transparency is used as the cut-out; no background "
                "removal is run.",
            )
        )
    return CutoutAssessment(
        background_removed=background_removed,
        coverage=round(coverage, 4),
        touches=touches,
        background_match=round(match, 4) if match is not None else None,
        warnings=warnings,
    )


def ai_view(rgb: np.ndarray, alpha: np.ndarray, *, size: int = PREVIEW_SIZE) -> Image.Image:
    """The square picture the AI receives: cropped to the mask, padded, on 50% grey.

    Follows TripoSR's ``resize_foreground`` (crop to every pixel with any alpha, pad to
    a square, then to ``FOREGROUND_RATIO``) and the generator's grey composite.
    """
    ys, xs = np.nonzero(alpha > 0)
    if len(ys) == 0:
        return Image.new("RGB", (size, size), (127, 127, 127))
    y1, y2, x1, x2 = ys.min(), ys.max(), xs.min(), xs.max()
    rgba = np.dstack([rgb, alpha])[y1:y2, x1:x2]  # TripoSR's crop (end-exclusive)
    side = max(rgba.shape[0], rgba.shape[1], 1)
    full = int(side / FOREGROUND_RATIO)
    canvas = np.zeros((full, full, 4), np.uint8)
    top = (full - rgba.shape[0]) // 2
    left = (full - rgba.shape[1]) // 2
    canvas[top : top + rgba.shape[0], left : left + rgba.shape[1]] = rgba
    picture = canvas.astype(np.float32) / 255.0
    composite = picture[..., :3] * picture[..., 3:] + (1.0 - picture[..., 3:]) * 0.5
    image = Image.fromarray((composite * 255.0).astype(np.uint8))
    return image.resize((size, size), Image.Resampling.LANCZOS)
