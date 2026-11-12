# mypy: ignore-errors
# (imports torch and TRELLIS, which exist only in the GPU image)
"""TRELLIS image-to-3D (Microsoft, MIT licence) on an NVIDIA GPU.

Only the mesh is decoded (``formats=["mesh"]``: FlexiCubes, Apache-2.0); the
Gaussian and radiance-field decoders and every renderer (nvdiffrast, Inria's
Gaussian rasteriser: non-commercial licences) are never used. Background removal
is rembg's U²-Net (MIT / Apache-2.0), as in TRELLIS's own preprocessing; the
framing is done by ``framing.frame`` so the app gets the same square with its
transparency.

This module runs only inside the GPU image (docs/GPU_SERVER.md). It is written
against TRELLIS's public pipeline API (``run`` / ``run_multi_image``,
``preprocess_image=False``) and has not been run on this project's CI, which has
no GPU.
"""

import os
import sys
import time
import types
from collections.abc import Callable

import numpy as np
from PIL import Image

from ai3d_gpu.backends.base import (
    CancelledError,
    Generated,
    GenerationSettings,
    Progress,
    ViewPhoto,
)
from ai3d_gpu.framing import frame

# TRELLIS works with Z up; glTF has Y up (TRELLIS's own to_glb uses this rotation).
Z_UP_TO_Y_UP = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=np.float64)
REMBG_MAX_SIDE = 1024  # TRELLIS shrinks photos to this before removing the background


def install_stand_ins() -> None:
    """Empty stand-ins for modules ``import trellis`` loads but the image-to-mesh path
    never calls, so they need not be installed: plyfile (GPL-3.0; saves Gaussians),
    open3d (text-to-3D only) and kaolin (FlexiCubes' input check only)."""
    for name in ("plyfile", "open3d"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["plyfile"].PlyData = sys.modules["plyfile"].PlyElement = None
    kaolin = types.ModuleType("kaolin")
    utils = types.ModuleType("kaolin.utils")
    testing = types.ModuleType("kaolin.utils.testing")
    testing.check_tensor = lambda *args, **kwargs: True  # only used inside asserts
    kaolin.utils, utils.testing = utils, testing
    for name, module in (
        ("kaolin", kaolin),
        ("kaolin.utils", utils),
        ("kaolin.utils.testing", testing),
    ):
        sys.modules.setdefault(name, module)


class TrellisBackend:
    name = "trellis"
    device = "cuda"

    def __init__(self, model_id: str, revision: str | None = None) -> None:
        os.environ.setdefault("SPCONV_ALGO", "native")  # no per-shape benchmarking
        install_stand_ins()
        import rembg
        import torch
        from huggingface_hub import snapshot_download
        from trellis.pipelines import TrellisImageTo3DPipeline

        if not torch.cuda.is_available():
            raise RuntimeError("TRELLIS needs an NVIDIA GPU with CUDA; none was found.")
        path = snapshot_download(model_id, revision=revision)
        self.pipeline = TrellisImageTo3DPipeline.from_pretrained(path)
        self.pipeline.cuda()
        self._on_gpu = True
        self.rembg_session = rembg.new_session("u2net")
        self._rembg = rembg
        self.model = f"{model_id}@{revision or 'latest'}"
        props = torch.cuda.get_device_properties(0)
        self.device = f"{props.name} ({props.total_memory / 2**30:.0f} GB)"

    def park(self) -> None:
        """Free the GPU for another model (the weights stay in memory)."""
        if self._on_gpu:
            import torch

            self.pipeline.cpu()
            self._on_gpu = False
            torch.cuda.empty_cache()

    def _cut_out(self, image: Image.Image) -> tuple[Image.Image, float]:
        if image.mode == "RGBA" and np.asarray(image.getchannel("A")).min() < 250:
            return image, 1.0
        rgb = image.convert("RGB")
        scale = min(1.0, REMBG_MAX_SIDE / max(rgb.size))
        if scale < 1.0:
            rgb = rgb.resize(
                (round(rgb.width * scale), round(rgb.height * scale)), Image.Resampling.LANCZOS
            )
        return self._rembg.remove(rgb, session=self.rembg_session), scale

    def generate(
        self,
        views: list[ViewPhoto],
        settings: GenerationSettings,
        progress: Progress,
        cancelled: Callable[[], bool],
    ) -> Generated:
        import torch

        timings: dict[str, float] = {}
        started = time.perf_counter()
        if not self._on_gpu:
            self.pipeline.cuda()
            self._on_gpu = True
        progress("preparing", 0.0)
        framed = {}
        for index, view in enumerate(views):
            cut, scale = self._cut_out(view.image)
            framed[view.role] = frame(cut, scale)
            progress("preparing", (index + 1) / len(views))
            if cancelled():
                raise CancelledError
        timings["preparing"] = time.perf_counter() - started

        progress("generating", 0.0)
        t = time.perf_counter()
        images = [item.model_input for item in framed.values()]
        structure = {"steps": settings.structure_steps, "cfg_strength": settings.structure_guidance}
        detail = {"steps": settings.detail_steps, "cfg_strength": settings.detail_guidance}
        with torch.no_grad():
            if len(images) == 1:
                outputs = self.pipeline.run(
                    images[0],
                    seed=settings.seed,
                    sparse_structure_sampler_params=structure,
                    slat_sampler_params=detail,
                    formats=["mesh"],
                    preprocess_image=False,
                )
            else:
                outputs = self.pipeline.run_multi_image(
                    images,
                    seed=settings.seed,
                    sparse_structure_sampler_params=structure,
                    slat_sampler_params=detail,
                    formats=["mesh"],
                    preprocess_image=False,
                    mode=settings.multi_view_mode,
                )
        timings["generating"] = time.perf_counter() - t
        if cancelled():
            raise CancelledError

        progress("meshing", 0.5)
        mesh = outputs["mesh"][0]
        vertices = mesh.vertices.detach().float().cpu().numpy().astype(np.float64) @ Z_UP_TO_Y_UP
        faces = mesh.faces.detach().cpu().numpy().astype(np.int64)
        colours = None
        if mesh.vertex_attrs is not None:
            colours = np.clip(mesh.vertex_attrs[:, :3].detach().float().cpu().numpy(), 0.0, 1.0)
        torch.cuda.empty_cache()
        progress("meshing", 1.0)
        return Generated(
            vertices=vertices,
            faces=faces,
            colours=colours,
            framed=framed,
            info={
                "seed": settings.seed,
                "views": len(images),
                "timings": {k: round(v, 2) for k, v in timings.items()},
            },
        )
