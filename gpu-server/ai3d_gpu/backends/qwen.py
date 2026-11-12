# mypy: ignore-errors
# (imports torch and transformers, which exist only in the GPU image)
"""Text from a prompt with Qwen3-4B-Instruct-2507 (Alibaba Qwen, Apache-2.0).

The app uses it to turn a scene prompt into a JSON scene plan; the instructions
come with each request, so the server holds no scene logic. The model loads on
first use (about 8 GB of GPU memory in bfloat16) and moves to the computer's
memory when another model needs the GPU (``park``).

Runs only inside the GPU image (docs/GPU_SERVER.md); written against the
transformers chat-template API and not run on this project's CI, which has no GPU.
"""

import threading
from collections.abc import Callable

from ai3d_gpu.backends.base import CancelledError, TextRequest


class QwenText:
    name = "qwen"
    device = "cuda"

    def __init__(self, model_id: str, revision: str | None) -> None:
        self.model = f"{model_id}@{revision or 'latest'}"
        self._model_id = model_id
        self._revision = revision
        self._loaded = None
        self._on_gpu = False
        self._lock = threading.Lock()

    def _load(self):
        if self._loaded is None:
            import torch
            from huggingface_hub import snapshot_download
            from transformers import AutoModelForCausalLM, AutoTokenizer

            if not torch.cuda.is_available():
                raise RuntimeError("The text model needs an NVIDIA GPU with CUDA; none was found.")
            path = snapshot_download(self._model_id, revision=self._revision)
            tokenizer = AutoTokenizer.from_pretrained(path)
            model = AutoModelForCausalLM.from_pretrained(path, torch_dtype=torch.bfloat16)
            model.eval()
            self._loaded = (tokenizer, model)
            props = torch.cuda.get_device_properties(0)
            self.device = f"{props.name} ({props.total_memory / 2**30:.0f} GB)"
        tokenizer, model = self._loaded
        if not self._on_gpu:
            model.to("cuda")
            self._on_gpu = True
        return tokenizer, model

    def park(self) -> None:
        """Free the GPU for another model (the weights stay in memory)."""
        with self._lock:
            if self._loaded is not None and self._on_gpu:
                import torch

                self._loaded[1].to("cpu")
                self._on_gpu = False
                torch.cuda.empty_cache()

    def write(self, request: TextRequest, cancelled: Callable[[], bool]) -> str:
        import torch

        with self._lock:
            tokenizer, model = self._load()
            if cancelled():
                raise CancelledError
            messages = []
            if request.system:
                messages.append({"role": "system", "content": request.system})
            messages.append({"role": "user", "content": request.prompt})
            text = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            inputs = tokenizer([text], return_tensors="pt").to("cuda")
            settings = request.settings
            torch.manual_seed(settings.seed)
            sampling = (
                {"do_sample": True, "temperature": settings.temperature, "top_p": settings.top_p}
                if settings.temperature > 0
                else {"do_sample": False}
            )
            with torch.no_grad():
                output = model.generate(
                    **inputs, max_new_tokens=settings.max_tokens, top_k=20, **sampling
                )
            reply = output[0][inputs["input_ids"].shape[1] :]
            return tokenizer.decode(reply, skip_special_tokens=True).strip()
