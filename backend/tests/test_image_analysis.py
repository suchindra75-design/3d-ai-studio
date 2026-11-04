import io
from pathlib import Path

import numpy as np
from PIL import Image

from ai3d.processing import images as ip
from tests import images


def _analyze(img: Image.Image, tmp_path: Path, fmt: str = "PNG") -> ip.ImageAnalysis:
    path = tmp_path / f"img.{fmt.lower()}"
    path.write_bytes(images.encode(img, fmt))
    with ip.open_validated(path, max_pixels=10**9) as opened:
        return ip.analyze(opened, ip.extract_metadata(opened))


def _codes(analysis: ip.ImageAnalysis) -> set[str]:
    return {w.code for w in analysis.warnings}


def test_sharp_texture_is_good(tmp_path: Path) -> None:
    analysis = _analyze(images.textured(), tmp_path)
    assert analysis.level == "good"
    assert analysis.sharpness > ip.SHARPNESS_WARN


def test_blur_is_detected(tmp_path: Path) -> None:
    sharp = _analyze(images.textured(), tmp_path)
    blurry = _analyze(images.blurred(), tmp_path)
    assert blurry.sharpness < sharp.sharpness / 10
    assert "blurry" in _codes(blurry)
    assert blurry.level == "poor"


def test_dark_and_flat_images(tmp_path: Path) -> None:
    dark = Image.fromarray(np.full((600, 800, 3), 10, dtype=np.uint8))
    analysis = _analyze(dark, tmp_path)
    assert {"underexposed", "low_texture", "blurry"} <= _codes(analysis)


def test_specular_highlights(tmp_path: Path) -> None:
    arr = np.asarray(images.textured()).copy()
    arr[:200, :] = 255
    assert "specular_highlights" in _codes(_analyze(Image.fromarray(arr), tmp_path))


def test_low_resolution(tmp_path: Path) -> None:
    analysis = _analyze(images.textured(320, 200), tmp_path)
    assert any(w.code == "low_resolution" and w.severity == "error" for w in analysis.warnings)


def test_alpha_subject_coverage_ignores_cutout_edge(tmp_path: Path) -> None:
    rgba = images.textured().convert("RGBA")
    alpha = np.zeros((600, 800), dtype=np.uint8)
    alpha[150:450, 200:600] = 255  # subject occupies 25% of the frame
    rgba.putalpha(Image.fromarray(alpha))
    analysis = _analyze(rgba, tmp_path)
    assert analysis.subject_coverage is not None
    assert abs(analysis.subject_coverage - 0.25) < 0.02
    assert analysis.level == "good"


def test_thumbnail_keeps_alpha() -> None:
    rgba = images.textured().convert("RGBA")
    alpha = np.zeros((600, 800), dtype=np.uint8)
    alpha[100:500, 100:700] = 255
    rgba.putalpha(Image.fromarray(alpha))
    data = ip.make_thumbnail(rgba, size=128, has_alpha=True)
    with Image.open(io.BytesIO(data)) as thumb:
        assert thumb.format == "WEBP"
        assert thumb.mode == "RGBA"
        assert max(thumb.size) == 128
