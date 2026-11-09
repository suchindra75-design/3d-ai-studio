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


def vase(
    background: tuple[int, int, int] = (215, 215, 210),
    colour: tuple[int, int, int] = (150, 70, 40),
    *,
    size: tuple[int, int] = (800, 600),
    centre: tuple[float, float] = (0.5, 0.5),
    scale: float = 1.0,
    noise: float = 6.0,
) -> tuple[Image.Image, np.ndarray]:
    """A shaded vase with a handle on a plain background, and its true mask.

    ``centre`` is a fraction of the picture; ``scale`` 1 makes the vase about two thirds
    of the picture's height.
    """
    width, height = size
    unit = height / 900 * scale
    cx, cy = centre[0] * width, centre[1] * height
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    body = ((xx - cx) / (220 * unit)) ** 2 + ((yy - cy) / (300 * unit)) ** 2 < 1
    neck = (np.abs(xx - cx) < 70 * unit) & (yy > cy - 420 * unit) & (yy < cy - 200 * unit)
    ring = np.abs(np.hypot(xx - (cx + 250 * unit), yy - cy) - 80 * unit) < 18 * unit
    mask = body | neck | (ring & (xx > cx + 180 * unit))
    shade = np.clip(1 - (xx - cx + 120 * unit) ** 2 / (2 * (220 * unit) ** 2), 0.0, 1.0)
    pixels = np.empty((height, width, 3), np.float32)
    pixels[:] = background
    pixels[mask] = np.asarray(colour, np.float32) * (0.35 + 0.65 * shade[mask])[:, None]
    pixels += np.random.default_rng(1).normal(0, noise, pixels.shape)
    return Image.fromarray(np.clip(pixels, 0, 255).astype("uint8")), mask


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
