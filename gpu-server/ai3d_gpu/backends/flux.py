# mypy: ignore-errors
# (imports torch and diffusers, which exist only in the GPU image)
"""Images from a prompt with FLUX.1-schnell (Black Forest Labs, Apache-2.0).

The app asks for one picture per object of a scene (a single object on a plain
background), which TRELLIS then turns into a 3D model. The model is gated on
Hugging Face: accept its terms once with your account and start the server with
``HF_TOKEN`` (docs/GPU_SERVER.md). It loads on first use. Its 12-billion-parameter
transformer alone needs about 24 GB in bfloat16, so with less than 40 GB of GPU
memory the layers are streamed from the computer's memory (slower, but it fits).

Runs only inside the GPU image; written against diffusers' FluxPipeline API and
not run on this project's CI, which has no GPU.
"""

import threading
from collections.abc import Callable

from PIL import Image

from ai3d_gpu.backends.base import CancelledError, ImageRequest, Progress

FULL_OFFLOAD_BELOW_GB = 40


class FluxImages:
    name = "flux"
    device = "cuda"

    def __init__(self, model_id: str, revision: str | None) -> None:
        self.model = f"{model_id}@{revision or 'latest'}"
        self._model_id = model_id
        self._revision = revision
        self._pipeline = None
        self._lock = threading.Lock()

    def _load(self):
        if self._pipeline is None:
            import torch
            from diffusers import FluxPipeline
            from huggingface_hub import snapshot_download
            from huggingface_hub.utils import GatedRepoError

            if not torch.cuda.is_available():
                raise RuntimeError("The image model needs an NVIDIA GPU with CUDA; none was found.")
            try:
                path = snapshot_download(self._model_id, revision=self._revision)
            except GatedRepoError as exc:
                raise RuntimeError(
                    f"{self._model_id} is gated: accept its terms on huggingface.co with your "
                    "account and start the server with HF_TOKEN set (docs/GPU_SERVER.md)."
                ) from exc
            pipeline = FluxPipeline.from_pretrained(path, torch_dtype=torch.bfloat16)
            props = torch.cuda.get_device_properties(0)
            memory_gb = props.total_memory / 2**30
            if memory_gb < FULL_OFFLOAD_BELOW_GB:
                pipeline.enable_sequential_cpu_offload()
            else:
                pipeline.enable_model_cpu_offload()
            self._pipeline = pipeline
            self.device = f"{props.name} ({memory_gb:.0f} GB)"
        return self._pipeline

    def park(self) -> None:
        """Nothing to do: the pipeline moves each part off the GPU after use."""

    def paint(
        self, request: ImageRequest, progress: Progress, cancelled: Callable[[], bool]
    ) -> Image.Image:
        import torch

        with self._lock:
            pipeline = self._load()
            settings = request.settings
            steps = settings.steps

            def step_done(pipe, step, timestep, kwargs):
                progress("painting", (step + 1) / steps)
                if cancelled():
                    raise CancelledError
                return kwargs

            progress("painting", 0.0)
            with torch.no_grad():
                result = pipeline(
                    request.prompt,
                    guidance_scale=0.0,
                    num_inference_steps=steps,
                    max_sequence_length=256,
                    width=settings.width,
                    height=settings.height,
                    generator=torch.Generator("cpu").manual_seed(settings.seed),
                    callback_on_step_end=step_done,
                )
            torch.cuda.empty_cache()
            return result.images[0].convert("RGB")
