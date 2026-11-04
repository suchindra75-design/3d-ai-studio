"""Image validation, metadata extraction, quality analysis and thumbnails.

Everything here is pure (no database, no storage) so it can be reused by the
upload service, the capture assistant and later the reconstruction pipelines.

Quality thresholds are heuristics tuned for photos downscaled to at most
``ANALYSIS_MAX_SIDE`` pixels; they flag likely problems, they do not prove them.
"""

import io
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

from ai3d.core.errors import InvalidInputError, PayloadTooLargeError, UnsupportedMediaTypeError

# Pillow format name -> (MIME type, file extension)
SUPPORTED_FORMATS: dict[str, tuple[str, str]] = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}
SUPPORTED_MIME_TYPES = tuple(mime for mime, _ext in SUPPORTED_FORMATS.values())

ANALYSIS_MAX_SIDE = 1024
SHARPNESS_POOR = 30.0
SHARPNESS_WARN = 100.0
MIN_SIDE_POOR = 256
MIN_SIDE_WARN = 512
UNDEREXPOSED = 0.15
OVEREXPOSED = 0.85
HIGHLIGHT_CLIP_WARN = 0.05
LOW_CONTRAST = 0.08

Severity = Literal["info", "warning", "error"]


@dataclass(slots=True)
class QualityWarning:
    code: str
    severity: Severity
    message: str


@dataclass(slots=True)
class ImageMetadata:
    format: str
    mime_type: str
    extension: str
    width: int  # after EXIF orientation
    height: int
    exif_orientation: int | None
    has_alpha: bool
    has_gps: bool
    camera_make: str | None = None
    camera_model: str | None = None
    focal_length_mm: float | None = None
    focal_length_35mm: float | None = None
    exif: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ImageAnalysis:
    sharpness: float
    brightness: float
    contrast: float
    clipped_highlights: float
    clipped_shadows: float
    megapixels: float
    subject_coverage: float | None  # opaque fraction for images with alpha
    warnings: list[QualityWarning]

    @property
    def level(self) -> Literal["good", "warning", "poor"]:
        severities = {w.severity for w in self.warnings}
        if "error" in severities:
            return "poor"
        if "warning" in severities:
            return "warning"
        return "good"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def open_validated(path: Path, *, max_pixels: int) -> Image.Image:
    """Open and fully decode an image, enforcing the supported-format allow-list.

    The format is detected from file content, never from the filename.
    """
    try:
        img = Image.open(path)
    except UnidentifiedImageError as exc:
        raise UnsupportedMediaTypeError(
            "File is not a recognised image. Supported formats: JPEG, PNG, WebP."
        ) from exc
    except Image.DecompressionBombError as exc:
        raise PayloadTooLargeError("Image dimensions are too large to process safely.") from exc

    if img.format not in SUPPORTED_FORMATS:
        img.close()
        raise UnsupportedMediaTypeError(
            f"Unsupported image format {img.format!r}. Supported formats: JPEG, PNG, WebP.",
            details={"detected_format": img.format},
        )
    if getattr(img, "is_animated", False):
        img.close()
        raise UnsupportedMediaTypeError("Animated images are not supported.")
    width, height = img.size
    if width * height > max_pixels:
        img.close()
        raise PayloadTooLargeError(
            f"Image has {width * height / 1e6:.0f} MP; the limit is {max_pixels / 1e6:.0f} MP.",
            details={"width": width, "height": height, "max_pixels": max_pixels},
        )
    try:
        img.load()
    except (OSError, SyntaxError) as exc:
        img.close()
        raise InvalidInputError(
            "Image data is corrupt or truncated.", code="corrupt_image"
        ) from exc
    return img


def _clean_text(value: Any, limit: int = 128) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    text = str(value).replace("\x00", "").strip()
    return text[:limit] or None


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return number if np.isfinite(number) and number > 0 else None


def _has_alpha(img: Image.Image) -> bool:
    return img.mode in {"RGBA", "LA", "PA"} or (img.mode == "P" and "transparency" in img.info)


def extract_metadata(img: Image.Image) -> ImageMetadata:
    """Format, orientation-corrected size and a curated EXIF subset (no GPS)."""
    assert img.format is not None
    mime, ext = SUPPORTED_FORMATS[img.format]
    exif = img.getexif()
    orientation = exif.get(ExifTags.Base.Orientation)
    width, height = img.size
    if orientation in {5, 6, 7, 8}:  # 90° rotations swap the axes
        width, height = height, width

    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
    has_gps = bool(exif.get_ifd(ExifTags.IFD.GPSInfo))

    curated: dict[str, Any] = {
        "date_time_original": _clean_text(exif_ifd.get(ExifTags.Base.DateTimeOriginal), 32),
        "exposure_time": _as_float(exif_ifd.get(ExifTags.Base.ExposureTime)),
        "f_number": _as_float(exif_ifd.get(ExifTags.Base.FNumber)),
        "iso": _as_float(exif_ifd.get(ExifTags.Base.ISOSpeedRatings)),
        "lens_model": _clean_text(exif_ifd.get(ExifTags.Base.LensModel)),
        "software": _clean_text(exif.get(ExifTags.Base.Software)),
    }
    return ImageMetadata(
        format=img.format,
        mime_type=mime,
        extension=ext,
        width=width,
        height=height,
        exif_orientation=int(orientation) if isinstance(orientation, int) else None,
        has_alpha=_has_alpha(img),
        has_gps=has_gps,
        camera_make=_clean_text(exif.get(ExifTags.Base.Make)),
        camera_model=_clean_text(exif.get(ExifTags.Base.Model)),
        focal_length_mm=_as_float(exif_ifd.get(ExifTags.Base.FocalLength)),
        focal_length_35mm=_as_float(exif_ifd.get(ExifTags.Base.FocalLengthIn35mmFilm)),
        exif={k: v for k, v in curated.items() if v is not None},
    )


def oriented(img: Image.Image) -> Image.Image:
    """Copy of the image with EXIF orientation applied (the original is untouched)."""
    return ImageOps.exif_transpose(img)


def analyze(img: Image.Image, meta: ImageMetadata) -> ImageAnalysis:
    """Cheap per-image quality signals for reconstruction suitability."""
    rgba = img.convert("RGBA") if meta.has_alpha else None
    gray = img.convert("L")
    gray.thumbnail((ANALYSIS_MAX_SIDE, ANALYSIS_MAX_SIDE), Image.Resampling.BILINEAR)
    pixels = np.asarray(gray, dtype=np.uint8)

    mask: np.ndarray | None = None
    coverage: float | None = None
    if rgba is not None:
        alpha = rgba.getchannel("A").resize(gray.size, Image.Resampling.BILINEAR)
        alpha_arr = np.asarray(alpha, dtype=np.uint8)
        coverage = float((alpha_arr > 0).mean())
        # Erode so the subject/transparent boundary does not count as detail.
        mask = cv2.erode((alpha_arr > 127).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0

    laplacian = cv2.Laplacian(pixels, cv2.CV_64F)
    region = pixels[mask] if mask is not None and mask.any() else pixels.ravel()
    lap_region = laplacian[mask] if mask is not None and mask.any() else laplacian.ravel()

    sharpness = float(lap_region.var())
    brightness = float(region.mean() / 255.0)
    contrast = float(region.std() / 255.0)
    clipped_high = float((region >= 250).mean())
    clipped_low = float((region <= 5).mean())

    warnings: list[QualityWarning] = []
    short_side = min(meta.width, meta.height)
    if short_side < MIN_SIDE_POOR:
        warnings.append(
            QualityWarning(
                "low_resolution",
                "error",
                f"Very low resolution ({meta.width}×{meta.height}). Use at least "
                f"{MIN_SIDE_WARN}px on the short side.",
            )
        )
    elif short_side < MIN_SIDE_WARN:
        warnings.append(
            QualityWarning(
                "low_resolution",
                "warning",
                f"Low resolution ({meta.width}×{meta.height}); fine detail will be lost.",
            )
        )

    if sharpness < SHARPNESS_POOR:
        warnings.append(
            QualityWarning(
                "blurry",
                "error",
                "Image looks blurry (camera shake or missed focus). Retake it if possible.",
            )
        )
    elif sharpness < SHARPNESS_WARN:
        warnings.append(
            QualityWarning(
                "soft_focus",
                "warning",
                "Image looks slightly soft. Sharper photos give better geometry.",
            )
        )

    if brightness < UNDEREXPOSED:
        warnings.append(
            QualityWarning(
                "underexposed", "warning", "Image is very dark; add even, diffuse light."
            )
        )
    elif brightness > OVEREXPOSED:
        warnings.append(
            QualityWarning(
                "overexposed", "warning", "Image is very bright; detail may be washed out."
            )
        )

    if clipped_high > HIGHLIGHT_CLIP_WARN:
        warnings.append(
            QualityWarning(
                "specular_highlights",
                "warning",
                f"{clipped_high:.0%} of pixels are blown-out highlights. Reflective or glossy "
                "surfaces reconstruct poorly; use diffuse light or a polarising filter.",
            )
        )

    if contrast < LOW_CONTRAST:
        warnings.append(
            QualityWarning(
                "low_texture",
                "warning",
                "Very low contrast. Plain, texture-less surfaces are hard to reconstruct.",
            )
        )

    if meta.has_gps:
        warnings.append(
            QualityWarning(
                "gps_metadata",
                "info",
                "Photo contains GPS location. The original is kept locally; location data "
                "is stripped from any copy sent to a cloud provider.",
            )
        )

    return ImageAnalysis(
        sharpness=round(sharpness, 2),
        brightness=round(brightness, 4),
        contrast=round(contrast, 4),
        clipped_highlights=round(clipped_high, 4),
        clipped_shadows=round(clipped_low, 4),
        megapixels=round(meta.width * meta.height / 1e6, 2),
        subject_coverage=round(coverage, 4) if coverage is not None else None,
        warnings=warnings,
    )


def make_thumbnail(img: Image.Image, *, size: int, has_alpha: bool) -> bytes:
    thumb = img.convert("RGBA" if has_alpha else "RGB")
    thumb.thumbnail((size, size), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    thumb.save(buffer, format="WEBP", quality=82, method=4)
    return buffer.getvalue()
