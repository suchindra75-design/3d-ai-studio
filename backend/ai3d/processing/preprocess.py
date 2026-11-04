"""Photo copies prepared for external providers.

The copy is upright (EXIF orientation applied), in sRGB, no larger than the
provider needs, and carries **no metadata at all** — no EXIF, GPS, camera
serials or embedded profiles. The original upload is never modified.
"""

import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageCms

from ai3d.processing.images import extract_metadata, open_validated, oriented

# Bump when the output changes so cached copies are regenerated.
PREPROCESS_VERSION = 1
JPEG_QUALITY = 95


@dataclass(frozen=True, slots=True)
class PreprocessedImage:
    data: bytes
    mime_type: str
    extension: str
    width: int
    height: int
    resized: bool
    color_converted: bool


def preprocess_signature(max_side: int) -> dict[str, int]:
    return {"version": PREPROCESS_VERSION, "max_side": max_side}


def _to_srgb(img: Image.Image) -> tuple[Image.Image, bool]:
    icc = img.info.get("icc_profile")
    if not icc:
        return img, False
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        target = ImageCms.createProfile("sRGB")
        mode = "RGBA" if img.mode in {"RGBA", "LA", "PA"} else "RGB"
        converted = ImageCms.profileToProfile(img.convert(mode), source, target, outputMode=mode)
    except (ImageCms.PyCMSError, OSError, ValueError):
        return img, False  # unusual profile: keep pixel values as they are
    return (converted if converted is not None else img), True


def _has_transparency(img: Image.Image) -> bool:
    if img.mode not in {"RGBA", "LA", "PA"} and not (
        img.mode == "P" and "transparency" in img.info
    ):
        return False
    alpha = img.convert("RGBA").getchannel("A")
    low = alpha.getextrema()[0]
    if isinstance(low, tuple):
        low = low[0]
    return float(low) < 255


def preprocess_for_provider(
    path: Path, *, max_pixels: int, max_side: int, max_bytes: int
) -> PreprocessedImage:
    with open_validated(path, max_pixels=max_pixels) as img:
        meta = extract_metadata(img)
        with oriented(img) as upright:
            transparent = meta.has_alpha and _has_transparency(upright)
            work, color_converted = _to_srgb(upright)
            work = work.convert("RGBA" if transparent else "RGB")
            # convert() copies ``info`` (EXIF, ICC, XMP…); some encoders reuse it.
            work.info.clear()
            resized = max(work.size) > max_side
            if resized:
                work.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            if transparent:
                # PNG keeps the cut-out; no text chunks or profiles are written.
                work.save(buffer, format="PNG", optimize=True)
                mime, ext = "image/png", "png"
            else:
                work.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
                mime, ext = "image/jpeg", "jpg"
            width, height = work.size
    data = buffer.getvalue()
    if len(data) > max_bytes:
        raise ValueError(f"Prepared image is {len(data)} bytes; the provider limit is {max_bytes}.")
    return PreprocessedImage(
        data=data,
        mime_type=mime,
        extension=ext,
        width=width,
        height=height,
        resized=resized,
        color_converted=color_converted,
    )
