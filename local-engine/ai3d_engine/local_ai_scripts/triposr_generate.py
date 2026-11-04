# mypy: ignore-errors
# (runs in the engine's own environment: torch, tsr and onnxruntime are not installed here)
"""Runs inside the local AI engine's own Python environment (not the backend's).

    <engine>/env/bin/python -I triposr_generate.py <request.json> <result.json>

The request (written by ``ai3d_engine.local_ai.engine``) holds only paths and
numbers. This script never downloads anything: TripoSR's Hugging Face lookups
are pointed at the files the installer verified, the checkpoint is loaded
tensors-only (no pickle code execution), and the result is a GLB in glTF
axes (Y up, facing +Z) with linear vertex colours, as glTF requires.

Progress is printed as ``PROGRESS <stage> <fraction>`` lines.
"""

import functools
import json
import sys
import time
import traceback
import types
from pathlib import Path


def progress(stage: str, fraction: float) -> None:
    print(f"PROGRESS {stage} {fraction:.3f}", flush=True)


def install_shims(dino_config: str) -> None:
    """Stand-ins for TripoSR's compiled/optional imports, and offline model lookup."""
    import mcubes
    import numpy as np
    import torch

    def marching_cubes(volume, threshold):  # torchmcubes' signature and output layout
        verts, faces = mcubes.marching_cubes(volume.cpu().numpy(), threshold)
        return (
            torch.from_numpy(verts.astype(np.float32)),
            torch.from_numpy(faces.astype(np.int64)),
        )

    shim = types.ModuleType("torchmcubes")
    shim.marching_cubes = marching_cubes
    sys.modules["torchmcubes"] = shim
    sys.modules["rembg"] = types.ModuleType("rembg")  # imported by tsr.utils, never used
    torch.load = functools.partial(torch.load, weights_only=True)

    import tsr.models.tokenizers.image as tokenizer

    def local_only(repo_id: str, filename: str, **_: object) -> str:
        if (repo_id, filename) != ("facebook/dino-vitb16", "config.json"):
            raise RuntimeError(f"unexpected model download: {repo_id}/{filename}")
        return dino_config

    tokenizer.hf_hub_download = local_only


def cut_out(image, u2net_path: str):
    """RGBA with the object's mask (U²-Net), unless the photo already has transparency."""
    import numpy as np
    import onnxruntime as ort
    from PIL import Image

    if image.mode == "RGBA" and image.getextrema()[3][0] < 250:
        return image, False
    rgb = image.convert("RGB")
    session = ort.InferenceSession(u2net_path, providers=["CPUExecutionProvider"])
    small = np.asarray(rgb.resize((320, 320), Image.LANCZOS), dtype=np.float32) / 255.0
    x = ((small - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]).transpose(2, 0, 1)[None]
    pred = session.run(None, {session.get_inputs()[0].name: x.astype(np.float32)})[0][0, 0]
    pred = (pred - pred.min()) / max(float(pred.max() - pred.min()), 1e-8)
    mask = Image.fromarray((pred * 255).astype(np.uint8)).resize(rgb.size, Image.LANCZOS)
    rgba = rgb.copy()
    rgba.putalpha(mask)
    return rgba, True


def to_gltf(mesh):
    """TripoSR axes (up +X, front +Z) to glTF (up +Y, front +Z); sRGB colours to linear."""
    import numpy as np

    rotate = np.eye(4)
    rotate[:3, :3] = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    mesh.apply_transform(rotate)
    colors = mesh.visual.vertex_colors
    srgb = colors[:, :3].astype(np.float64) / 255.0
    linear = np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)
    colors[:, :3] = np.round(linear * 255.0).astype(np.uint8)
    mesh.visual.vertex_colors = colors
    return mesh


def main(request_path: str, result_path: str) -> None:
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    timings: dict[str, float] = {}
    sys.path.insert(0, request["source_dir"])
    started = time.perf_counter()
    progress("starting", 0.0)
    install_shims(request["dino_config"])

    import numpy as np
    import torch
    from PIL import Image
    from tsr.system import TSR
    from tsr.utils import resize_foreground

    torch.set_num_threads(int(request.get("threads") or torch.get_num_threads()))
    device = "cuda" if request.get("device") != "cpu" and torch.cuda.is_available() else "cpu"

    progress("background", 0.0)
    t = time.perf_counter()
    with Image.open(request["image"]) as opened:
        opened.load()
        image, removed = cut_out(opened, request["u2net"])
    alpha = np.asarray(image.getchannel("A"))
    if (alpha > 127).mean() < 0.01:
        raise ValueError("No object was found in the photo (background removal left nothing).")
    image = resize_foreground(image, float(request.get("foreground_ratio", 0.85)))
    rgba = np.asarray(image).astype(np.float32) / 255.0
    rgb = rgba[:, :, :3] * rgba[:, :, 3:4] + (1 - rgba[:, :, 3:4]) * 0.5
    prepared = Image.fromarray((rgb * 255.0).astype(np.uint8))
    prepared.save(request["prepared_image"])
    timings["background"] = time.perf_counter() - t

    progress("loading", 0.0)
    t = time.perf_counter()
    model = TSR.from_pretrained(
        request["weights_dir"], config_name="config.yaml", weight_name="model.ckpt"
    )
    model.renderer.set_chunk_size(int(request.get("chunk_size", 8192)))
    model.to(device)
    timings["load"] = time.perf_counter() - t

    progress("generating", 0.0)
    t = time.perf_counter()
    with torch.no_grad():
        scene_codes = model([prepared], device=device)
    timings["generating"] = time.perf_counter() - t

    progress("meshing", 0.0)
    t = time.perf_counter()
    with torch.no_grad():
        mesh = model.extract_mesh(scene_codes, True, resolution=int(request["resolution"]))[0]
    mesh = to_gltf(mesh)
    mesh.export(request["output"], file_type="glb")
    timings["meshing"] = time.perf_counter() - t

    result = {
        "ok": True,
        "device": device,
        "threads": torch.get_num_threads(),
        "background_removed": removed,
        "vertices": len(mesh.vertices),
        "triangles": len(mesh.faces),
        "timings": {k: round(v, 2) for k, v in timings.items()},
        "seconds": round(time.perf_counter() - started, 2),
    }
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    request_file, result_file = sys.argv[1], sys.argv[2]
    try:
        main(request_file, result_file)
    except Exception as exc:  # reported to the caller, with the traceback in the log
        traceback.print_exc()
        message = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
        Path(result_file).write_text(json.dumps({"ok": False, "error": message}))
        sys.exit(1)
