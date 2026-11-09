# mypy: ignore-errors
# (runs in the engine's own environment: onnxruntime is not installed here)
"""Runs inside the local AI engine's own Python environment (not the backend's).

    <engine>/env/bin/python -I cutout_preview.py <request.json> <result.json>

Makes the same cut-out that local generation makes (``triposr_generate.cut_out``)
and saves its mask as a greyscale PNG, so the app can show what the AI will see
before anything is generated. Needs only onnxruntime, numpy and Pillow; nothing
is downloaded.
"""

import importlib.util
import json
import sys
import traceback
from pathlib import Path


def _generator():
    """triposr_generate.py from this folder (``-I`` leaves this folder off sys.path)."""
    path = Path(__file__).resolve().with_name("triposr_generate.py")
    spec = importlib.util.spec_from_file_location("triposr_generate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(request_path: str, result_path: str) -> None:
    from PIL import Image

    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    with Image.open(request["image"]) as opened:
        opened.load()
        image, removed = _generator().cut_out(opened, request["u2net"])
    image.getchannel("A").save(request["mask"], format="PNG")
    result = {
        "ok": True,
        "background_removed": removed,
        "width": image.width,
        "height": image.height,
    }
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    request_file, result_file = sys.argv[1], sys.argv[2]
    try:
        main(request_file, result_file)
    except Exception as exc:  # reported to the caller, with the traceback in the log
        traceback.print_exc()
        Path(result_file).write_text(
            json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        )
        sys.exit(1)
