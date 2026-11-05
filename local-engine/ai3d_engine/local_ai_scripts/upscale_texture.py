# mypy: ignore-errors
"""AI 3D Studio's texture upscaler. Runs in the local AI engine's own environment.

Started only by :mod:`ai3d_engine.local_ai.engine` as::

    <engine>/env/bin/python -I upscale_texture.py <request.json> <result.json>

The request names one image, the output path, the scale (2 or 4), the largest
side allowed and the weights file. The network is Real-ESRGAN's compact model
(SRVGGNetCompact, BSD-3-Clause), written out here; the weights are loaded as
plain tensors (``weights_only=True``), so no code from the file runs. Images are
processed in overlapping tiles to bound memory. Alpha is resized conventionally.

An upscaler invents plausible fine detail; it cannot recover detail the
original texture never had.
"""

import json
import sys
import time
from pathlib import Path

TILE = 192  # default; the request may choose 32–2048
PAD = 12
SCALE = 4


def progress(fraction: float) -> None:
    print(f"PROGRESS upscaling {fraction:.3f}", flush=True)


def build_model(torch):
    nn = torch.nn

    class SRVGGNetCompact(nn.Module):
        """Real-ESRGAN's compact network: conv + PReLU stack, pixel shuffle, skip."""

        def __init__(self, num_feat: int = 64, num_conv: int = 32) -> None:
            super().__init__()
            layers = [nn.Conv2d(3, num_feat, 3, 1, 1), nn.PReLU(num_parameters=num_feat)]
            for _ in range(num_conv):
                layers += [
                    nn.Conv2d(num_feat, num_feat, 3, 1, 1),
                    nn.PReLU(num_parameters=num_feat),
                ]
            layers.append(nn.Conv2d(num_feat, 3 * SCALE * SCALE, 3, 1, 1))
            self.body = nn.ModuleList(layers)
            self.upsampler = nn.PixelShuffle(SCALE)

        def forward(self, x):
            out = x
            for layer in self.body:
                out = layer(out)
            out = self.upsampler(out)
            return out + torch.nn.functional.interpolate(x, scale_factor=SCALE, mode="nearest")

    return SRVGGNetCompact()


def upscale(model, torch, rgb, out_height, out_width, *, tile=TILE):
    """rgb: float tensor (1, 3, H, W) in 0..1 → uint8 array (out_height, out_width, 3).

    Each tile is run ×4 and immediately resized to its share of the output, so
    memory follows the output size, not 4× the input.
    """
    import numpy as np

    _, _, height, width = rgb.shape
    fy, fx = out_height / height, out_width / width
    out = np.zeros((out_height, out_width, 3), dtype=np.uint8)
    tiles = [(y, x) for y in range(0, height, tile) for x in range(0, width, tile)]
    for n, (y, x) in enumerate(tiles):
        th, tw = min(tile, height - y), min(tile, width - x)
        y0, x0 = max(y - PAD, 0), max(x - PAD, 0)
        y1, x1 = min(y + th + PAD, height), min(x + tw + PAD, width)
        with torch.no_grad():
            result = model(rgb[:, :, y0:y1, x0:x1])
        ty, tx = (y - y0) * SCALE, (x - x0) * SCALE
        piece = result[:, :, ty : ty + th * SCALE, tx : tx + tw * SCALE].clamp(0, 1)
        dy0, dy1 = round(y * fy), round((y + th) * fy)
        dx0, dx1 = round(x * fx), round((x + tw) * fx)
        if (dy1 - dy0, dx1 - dx0) != tuple(piece.shape[2:]):
            piece = torch.nn.functional.interpolate(piece, size=(dy1 - dy0, dx1 - dx0), mode="area")
        out[dy0:dy1, dx0:dx1] = np.round(piece[0].numpy().transpose(1, 2, 0) * 255.0).astype(
            np.uint8
        )
        progress((n + 1) / len(tiles))
    return out


def main(request_path: str, result_path: str) -> None:
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    started = time.perf_counter()
    import numpy as np
    import torch
    from PIL import Image

    scale = int(request["scale"])
    max_size = int(request["max_size"])
    tile = int(request.get("tile", TILE))
    if scale not in {2, 4} or not 64 <= max_size <= 16384 or not 32 <= tile <= 2048:
        raise ValueError("Invalid scale, maximum size or tile size.")
    torch.set_grad_enabled(False)
    model = build_model(torch)
    state = torch.load(request["weights"], map_location="cpu", weights_only=True)
    model.load_state_dict(state.get("params", state), strict=True)
    model.eval()

    source = Image.open(request["input"])
    source.load()
    alpha = source.getchannel("A") if "A" in source.getbands() else None
    rgb = np.asarray(source.convert("RGB"), dtype=np.float32) / 255.0
    tensor = torch.from_numpy(rgb.transpose(2, 0, 1).copy()).unsqueeze(0)
    width, height = source.width * scale, source.height * scale
    factor = min(1.0, max_size / max(width, height))
    size = (max(1, round(width * factor)), max(1, round(height * factor)))
    image = Image.fromarray(upscale(model, torch, tensor, size[1], size[0], tile=tile), "RGB")
    if alpha is not None:
        image.putalpha(alpha.resize(size, Image.LANCZOS))
    image.save(request["output"], format="PNG")
    Path(result_path).write_text(
        json.dumps(
            {
                "ok": True,
                "width": size[0],
                "height": size[1],
                "seconds": round(time.perf_counter() - started, 2),
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    request_file, result_file = sys.argv[1], sys.argv[2]
    try:
        main(request_file, result_file)
    except Exception as exc:  # reported to the backend as a failure
        Path(result_file).write_text(json.dumps({"ok": False, "error": str(exc)[:500]}))
        raise
