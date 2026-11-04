"""Synthetic multi-view photo sets for photogrammetry tests.

A ray caster (NumPy only) renders a textured box standing on a textured floor
from cameras orbiting it, like someone walking around an object with a phone.
The geometry is known exactly, so tests can check what COLMAP recovers. These
are test fixtures; nothing here is used by the application.
"""

import io
import math
from dataclasses import dataclass
from functools import cache

import numpy as np
from PIL import Image, ImageFilter

# Box half-extents (x, y, z); it stands on the floor (y = 0).
BOX = np.array([0.5, 0.35, 0.3])


@dataclass(frozen=True)
class View:
    azimuth_deg: float
    elevation_deg: float


@cache
def _texture(seed: int, size: int = 512) -> np.ndarray:
    """Multi-scale noise: plenty of distinctive features for SIFT."""
    rng = np.random.default_rng(seed)
    out = np.zeros((size, size, 3), dtype=np.float32)
    for scale, weight in ((8, 0.45), (32, 0.3), (128, 0.25)):
        small = rng.random((scale, scale, 3)).astype(np.float32)
        layer = Image.fromarray((small * 255).astype(np.uint8)).resize(
            (size, size), Image.Resampling.BICUBIC
        )
        out += np.asarray(layer, dtype=np.float32) / 255 * weight
    sharp = Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8)).filter(
        ImageFilter.UnsharpMask(radius=2, percent=150)
    )
    return np.asarray(sharp, dtype=np.float32) / 255


def orbit(count: int, elevation_deg: float, start_deg: float = 0.0) -> list[View]:
    return [View(start_deg + 360 * i / count, elevation_deg) for i in range(count)]


def default_views() -> list[View]:
    """30 photos: two rings around the object, the way a capture guide asks for."""
    return orbit(18, 20.0) + orbit(12, 45.0, start_deg=15.0)


def render(view: View, *, width: int = 640, height: int = 480, focal: float = 560.0) -> np.ndarray:
    textures = [_texture(seed) for seed in range(7)]  # 6 box faces + floor
    az, el = math.radians(view.azimuth_deg), math.radians(view.elevation_deg)
    distance = 2.4
    target = np.array([0.0, BOX[1], 0.0])
    eye = target + distance * np.array(
        [math.cos(el) * math.sin(az), math.sin(el), math.cos(el) * math.cos(az)]
    )
    forward = (target - eye) / np.linalg.norm(target - eye)
    right = np.cross(forward, [0.0, 1.0, 0.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)

    u, v = np.meshgrid(np.arange(width) + 0.5, np.arange(height) + 0.5)
    dirs = (
        ((u - width / 2) / focal)[..., None] * right
        - ((v - height / 2) / focal)[..., None] * up
        + forward
    )
    dirs /= np.linalg.norm(dirs, axis=-1, keepdims=True)

    image = np.full((height, width, 3), 0.08, dtype=np.float32)  # background
    depth = np.full((height, width), np.inf)
    light = np.array([0.4, 0.8, 0.45])
    light /= np.linalg.norm(light)

    # Floor (y = 0), a 3 m square.
    with np.errstate(divide="ignore", invalid="ignore"):
        t = -eye[1] / dirs[..., 1]
    hit = eye + t[..., None] * dirs
    on_floor = (t > 0) & (np.abs(hit[..., 0]) < 1.5) & (np.abs(hit[..., 2]) < 1.5)
    tex = textures[6]
    fu = ((hit[..., 0] + 1.5) / 3 * (tex.shape[1] - 1)).clip(0, tex.shape[1] - 1).astype(int)
    fv = ((hit[..., 2] + 1.5) / 3 * (tex.shape[0] - 1)).clip(0, tex.shape[0] - 1).astype(int)
    image[on_floor] = tex[fv[on_floor], fu[on_floor]] * 0.75
    depth[on_floor] = t[on_floor]

    # Box: slab intersection.
    lo = np.array([-BOX[0], 0.0, -BOX[2]])
    hi = np.array([BOX[0], 2 * BOX[1], BOX[2]])
    with np.errstate(divide="ignore", invalid="ignore"):
        t1 = (lo - eye) / dirs
        t2 = (hi - eye) / dirs
    tnear = np.minimum(t1, t2).max(axis=-1)
    tfar = np.maximum(t1, t2).min(axis=-1)
    on_box = (tnear <= tfar) & (tnear > 0) & (tnear < depth)
    point = eye + tnear[..., None] * dirs
    axis = np.minimum(t1, t2).argmax(axis=-1)
    sign = np.where(dirs[np.arange(height)[:, None], np.arange(width)[None, :], axis] > 0, 0, 1)
    face = axis * 2 + sign
    normals = np.zeros((6, 3))
    for f in range(6):
        normals[f, f // 2] = 1.0 if f % 2 else -1.0
    for f in range(6):
        mask = on_box & (face == f)
        if not mask.any():
            continue
        a, b = [i for i in range(3) if i != f // 2]
        span_a, span_b = hi[a] - lo[a], hi[b] - lo[b]
        tex = textures[f]
        tu = ((point[..., a] - lo[a]) / span_a * (tex.shape[1] - 1)).clip(0, tex.shape[1] - 1)
        tv = ((point[..., b] - lo[b]) / span_b * (tex.shape[0] - 1)).clip(0, tex.shape[0] - 1)
        shade = 0.45 + 0.55 * max(float(normals[f] @ light), 0.0)
        image[mask] = tex[tv[mask].astype(int), tu[mask].astype(int)] * shade
    return (np.clip(image, 0, 1) * 255).astype(np.uint8)


def jpeg(pixels: np.ndarray, quality: int = 92) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


def photo_set(views: list[View] | None = None, **kwargs: int | float) -> list[tuple[str, bytes]]:
    return [
        (f"view-{i:02d}.jpg", jpeg(render(view, **kwargs)))  # type: ignore[arg-type]
        for i, view in enumerate(views or default_views())
    ]
