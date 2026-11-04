"""Synthetic test images (no binary fixtures in the repository)."""

import io

import numpy as np
from PIL import Image, ImageFilter


def textured(width: int = 800, height: int = 600, seed: int = 0) -> Image.Image:
    """High-frequency texture: sharp and feature-rich."""
    rng = np.random.default_rng(seed)
    base = (rng.random((height // 8, width // 8, 3)) * 255).astype("uint8")
    img = Image.fromarray(base).resize((width, height), Image.Resampling.NEAREST)
    noise = (rng.random((height, width, 3)) * 60).astype("uint8")
    return Image.fromarray(
        np.clip(np.asarray(img, dtype=np.int16) + noise - 30, 0, 255).astype("uint8")
    )


def blurred(width: int = 800, height: int = 600, seed: int = 0) -> Image.Image:
    return textured(width, height, seed).filter(ImageFilter.GaussianBlur(12))


def encode(img: Image.Image, fmt: str = "JPEG", **kwargs: object) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def jpeg_with_exif(
    img: Image.Image,
    *,
    orientation: int | None = None,
    gps: bool = False,
    make: str = "TestCam",
    model: str = "X100",
    focal: float = 23.0,
) -> bytes:
    exif = Image.Exif()
    exif[0x010F] = make
    exif[0x0110] = model
    if orientation:
        exif[0x0112] = orientation
    exif_ifd = exif.get_ifd(0x8769)
    exif_ifd[0x920A] = focal  # FocalLength
    exif_ifd[0x9003] = "2026:10:01 12:00:00"  # DateTimeOriginal
    if gps:
        gps_ifd = exif.get_ifd(0x8825)
        gps_ifd[1] = "N"
        gps_ifd[2] = (52.0, 22.0, 0.0)
    return encode(img, "JPEG", exif=exif, quality=90)
