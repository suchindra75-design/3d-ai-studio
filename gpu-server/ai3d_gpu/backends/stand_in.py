"""Backends for trying the server without a GPU (and for tests).

They run no AI model. The 3D stand-in returns an ellipsoid with the front photo's
proportions and its average colour; the text stand-in answers a request for a JSON
scene plan with one object per phrase of the prompt; the image stand-in paints a
plain coloured shape. Everything around them (upload, queue, framing, results, and
the app's provider, refinement, scene layout and rendering) runs for real.
"""

import hashlib
import json
import re
from collections.abc import Callable

import numpy as np
import trimesh
from PIL import Image, ImageDraw

from ai3d_gpu.backends.base import (
    CancelledError,
    Generated,
    GenerationSettings,
    ImageRequest,
    Progress,
    TextRequest,
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


_SPLIT = re.compile(r",|;|\band\b|\bwith\b|\bnext to\b|\bbeside\b|\bnear\b", re.IGNORECASE)
_ARTICLE = re.compile(r"^(?:a|an|the|some|two|three)\s+", re.IGNORECASE)


class StandInText:
    """No language model: a JSON plan with one object per phrase (at most four)."""

    name = "stand-in"
    model = "stand-in text (no AI model)"
    device = "cpu"

    def write(self, request: TextRequest, cancelled: Callable[[], bool]) -> str:
        if cancelled():
            raise CancelledError
        if "json" not in request.system.lower():
            return request.prompt
        phrases = [_ARTICLE.sub("", p.strip(" .")) for p in _SPLIT.split(request.prompt)]
        names = [p for p in phrases if p][:4] or ["object"]
        return json.dumps(
            {
                "title": request.prompt.strip()[:60],
                "objects": [{"name": n[:60], "description": n} for n in names],
            }
        )


class StandInImage:
    """No image model: a coloured ellipse (colour and proportions from the prompt)
    on a plain light background, which the 3D stand-in turns into a coloured shape."""

    name = "stand-in"
    model = "stand-in image (no AI model)"
    device = "cpu"

    def paint(
        self, request: ImageRequest, progress: Progress, cancelled: Callable[[], bool]
    ) -> Image.Image:
        if cancelled():
            raise CancelledError
        progress("painting", 0.0)
        digest = hashlib.sha256(request.prompt.encode()).digest()
        colour = (40 + digest[0] % 180, 40 + digest[1] % 180, 40 + digest[2] % 180)
        width, height = request.settings.width, request.settings.height
        image = Image.new("RGB", (width, height), (236, 236, 236))
        aspect = 0.5 + (digest[3] % 100) / 100  # width / height of the shape
        box_h = height * 0.6
        box_w = min(width * 0.8, box_h * aspect)
        left, top = (width - box_w) / 2, (height - box_h) / 2
        ImageDraw.Draw(image).ellipse((left, top, left + box_w, top + box_h), fill=colour)
        progress("painting", 1.0)
        return image
