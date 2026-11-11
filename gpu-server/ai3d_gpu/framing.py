"""Framing photos the way TRELLIS does, keeping the transparency.

TRELLIS crops each cut-out photo to a square 1.2 × the object's size (around the
object's middle), scales it to 518 px and composites it on black. The app needs
the same square *with* its transparency to project the photo back onto the model,
so the framing is done here once, and both versions come from it.
"""

from dataclasses import dataclass

import numpy as np
from PIL import Image

MODEL_SIZE = 518
FRAME_RATIO = 1.2  # square side / object size (TRELLIS preprocess_image)
OBJECT_ALPHA = 0.8 * 255  # alpha above which a pixel counts as the object
CUTOUT_MAX = 2048


class NoObjectError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Framed:
    cutout: Image.Image  # RGBA square, at most CUTOUT_MAX px
    model_input: Image.Image  # RGB 518² on black, as TRELLIS expects
    side: float  # the square's side in pixels of the photo as given


def frame(rgba: Image.Image, scale: float = 1.0) -> Framed:
    """Frame a cut-out photo. ``scale``: how much the photo was shrunk before the
    background was removed (the square's side is reported in the original's pixels)."""
    alpha = np.asarray(rgba.getchannel("A"))
    found = np.argwhere(alpha > OBJECT_ALPHA)
    if len(found) == 0:
        raise NoObjectError("No object was found in the photo (background removal left nothing).")
    top, left = found.min(axis=0)
    bottom, right = found.max(axis=0)
    centre = ((left + right) / 2, (top + bottom) / 2)
    size = int(max(right - left, bottom - top) * FRAME_RATIO)
    box = (
        int(centre[0] - size // 2),
        int(centre[1] - size // 2),
        int(centre[0] + size // 2),
        int(centre[1] + size // 2),
    )
    square = rgba.crop(box)  # outside the photo: transparent
    side = min(square.width, CUTOUT_MAX)
    cutout = square.resize((side, side), Image.Resampling.LANCZOS)
    small = np.asarray(
        square.resize((MODEL_SIZE, MODEL_SIZE), Image.Resampling.LANCZOS), dtype=np.float32
    )
    rgb = small[:, :, :3] * (small[:, :, 3:4] / 255.0)
    model_input = Image.fromarray(np.round(rgb).astype(np.uint8), mode="RGB")
    return Framed(cutout=cutout, model_input=model_input, side=square.width / scale)
