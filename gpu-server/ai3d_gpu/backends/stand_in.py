"""A backend for trying the server without a GPU (and for tests).

It makes no real 3D model: it returns an ellipsoid with the front photo's
proportions and its average colour. Everything around it (upload, queue, framing,
results, the app's provider and refinement) runs for real.
"""

from collections.abc import Callable

import numpy as np
import trimesh
from PIL import Image

from ai3d_gpu.backends.base import (
    CancelledError,
    Generated,
    GenerationSettings,
    Progress,
    ViewPhoto,
)
from ai3d_gpu.framing import Framed, frame


def cut_out_plain(image: Image.Image) -> Image.Image:
    """RGBA: the photo's own transparency, or everything that differs from the
    border's median colour (good enough for plain backgrounds)."""
    if image.mode == "RGBA" and np.asarray(image.getchannel("A")).min() < 250:
        return image
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    background = np.median(border, axis=0)
    alpha = (np.abs(rgb - background).max(axis=2) > 40).astype(np.uint8) * 255
    return Image.fromarray(np.dstack([rgb.astype(np.uint8), alpha]), mode="RGBA")


class StandInBackend:
    name = "stand-in"
    model = "stand-in ellipsoid (no AI model)"
    device = "cpu"

    def generate(
        self,
        views: list[ViewPhoto],
        settings: GenerationSettings,
        progress: Progress,
        cancelled: Callable[[], bool],
    ) -> Generated:
        progress("preparing", 0.0)
        framed: dict[str, Framed] = {
            str(view.role): frame(cut_out_plain(view.image)) for view in views
        }
        if cancelled():
            raise CancelledError
        progress("generating", 0.5)
        front = next(iter(framed.values())).cutout
        alpha = np.asarray(front.getchannel("A")) > 127
        rows, cols = np.nonzero(alpha)
        height = (rows.max() - rows.min() + 1) / front.height
        width = (cols.max() - cols.min() + 1) / front.width
        shape = trimesh.creation.icosphere(subdivisions=4, radius=1.0)
        scale = 0.5 / max(height, width)
        shape.vertices = shape.vertices * np.array([width, height, min(width, height)]) * scale
        colour = np.asarray(front.convert("RGB"))[alpha].mean(axis=0) / 255.0
        progress("meshing", 1.0)
        return Generated(
            vertices=np.asarray(shape.vertices),
            faces=np.asarray(shape.faces),
            colours=np.tile(colour, (len(shape.vertices), 1)),
            framed=framed,
            info={"seed": settings.seed},
        )
