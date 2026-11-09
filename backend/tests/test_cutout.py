"""Cut-out preview: the checks on a mask, the upload-time framing check, the API, and
(with AI3D_LOCAL_AI_DIR) the real background removal of the installed engine.

API tests replace only the engine's ``cut_out`` call with a stand-in that writes the
true mask of a synthetic picture, so the provider copy, checks, storage and capture
assessment all run for real.
"""

import io
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ai3d.processing import cutout
from ai3d.processing import images as ip
from ai3d_engine.local_ai import engine
from tests import images
from tests.conftest import stored_keys

MakeProject = Callable[..., dict[str, Any]]
Upload = Callable[..., Any]
REAL_ENGINE = os.environ.get("AI3D_LOCAL_AI_DIR")


def _alpha(mask: np.ndarray) -> np.ndarray:
    return np.where(mask, 255, 0).astype(np.uint8)


def _assess(picture: Image.Image, mask: np.ndarray) -> cutout.CutoutAssessment:
    return cutout.assess(np.asarray(picture), _alpha(mask), background_removed=True)


def _codes(found: Any) -> set[str]:
    return {w.code if hasattr(w, "code") else w["code"] for w in found}


# -- the checks ---------------------------------------------------------------------


def test_a_framed_object_on_a_contrasting_background_has_no_warnings() -> None:
    result = _assess(*images.vase())
    assert result.warnings == []
    assert result.touches == []
    assert result.background_match is not None
    assert result.background_match < 0.3
    assert 0.15 < result.coverage < 0.35


def test_an_object_running_off_the_photo_is_reported_by_edge() -> None:
    result = _assess(*images.vase(centre=(0.5, 0.85)))
    assert result.touches == ["bottom"]
    (warning,) = result.warnings
    assert warning.code == "subject_cropped"
    assert "bottom edge" in warning.message

    picture, mask = images.vase(centre=(0.05, 0.05))
    assert _assess(picture, mask).touches == ["top", "left"]
    assert "top and left edges" in _assess(picture, mask).warnings[0].message


def test_a_stray_pixel_on_the_edge_does_not_count() -> None:
    mask = np.zeros((100, 200), bool)
    mask[0, 50] = True
    assert ip.frame_touches(mask) == []
    mask[:2, 50:60] = True  # 5% of the top edge
    assert ip.frame_touches(mask) == ["top"]


def test_an_object_that_blends_into_the_background_is_reported() -> None:
    # A dark object on a dark backdrop, as in an illustration of a dark-suited figure.
    result = _assess(*images.vase((18, 16, 18), (40, 36, 40)))
    assert result.background_match is not None
    assert result.background_match >= cutout.BACKGROUND_MATCH_WARN
    assert _codes(result.warnings) == {"low_edge_contrast"}
    # A light object on a dark backdrop is fine.
    assert _assess(*images.vase((30, 30, 35), (230, 220, 200))).warnings == []


def test_small_and_missing_objects() -> None:
    small = _assess(*images.vase(scale=0.3))
    assert _codes(small.warnings) == {"subject_small"}

    picture, mask = images.vase()
    empty = _assess(picture, np.zeros_like(mask))
    assert _codes(empty.warnings) == {"no_subject"}
    assert empty.warnings[0].severity == "error"
    assert empty.background_match is None


def test_own_transparency_is_noted_and_not_scored_for_contrast() -> None:
    picture, mask = images.vase()
    result = cutout.assess(np.asarray(picture), _alpha(mask), background_removed=False)
    assert result.background_match is None
    assert _codes(result.warnings) == {"own_transparency"}
    assert result.warnings[0].severity == "info"


def test_the_ai_view_is_the_generators_framing() -> None:
    picture, mask = images.vase(centre=(0.3, 0.5))
    view = np.asarray(cutout.ai_view(np.asarray(picture), _alpha(mask), size=256))
    assert view.shape == (256, 256, 3)
    assert (np.abs(view[0, 0].astype(int) - 127) <= 1).all()  # plain grey around it
    # The object is centred and its larger side fills FOREGROUND_RATIO of the view.
    grey = (np.abs(view.astype(int) - 127) <= 3).all(axis=2)
    ys, xs = np.nonzero(~grey)
    assert abs((ys.max() - ys.min()) / 256 - cutout.FOREGROUND_RATIO) < 0.03
    assert abs((ys.min() + ys.max()) / 2 - 128) < 4
    assert abs((xs.min() + xs.max()) / 2 - 128) < 4


# -- upload-time framing check for photos with their own transparency ---------------


def _png_with_alpha(mask: np.ndarray) -> bytes:
    picture, _ = images.vase(size=(mask.shape[1], mask.shape[0]))
    rgba = picture.convert("RGBA")
    rgba.putalpha(Image.fromarray(_alpha(mask)))
    return images.encode(rgba, "PNG")


def _analysis(data: bytes, tmp_path: Path) -> ip.ImageAnalysis:
    path = tmp_path / "photo.png"
    path.write_bytes(data)
    with ip.open_validated(path, max_pixels=10**9) as opened:
        return ip.analyze(opened, ip.extract_metadata(opened))


def test_a_cut_out_photo_running_off_the_frame_is_flagged_at_upload(tmp_path: Path) -> None:
    _, framed = images.vase()
    _, cropped = images.vase(centre=(0.5, 0.1))
    assert "subject_cropped" not in _codes(_analysis(_png_with_alpha(framed), tmp_path).warnings)
    found = _analysis(_png_with_alpha(cropped), tmp_path)
    assert "subject_cropped" in _codes(found.warnings)
    assert "top edge" in next(w for w in found.warnings if w.code == "subject_cropped").message
    empty = _analysis(_png_with_alpha(np.zeros_like(framed)), tmp_path)
    assert "no_subject" in _codes(empty.warnings)
    assert empty.level == "poor"


# -- the API and the capture check --------------------------------------------------


def _stand_in(calls: list[Path], mask: np.ndarray) -> Callable[..., dict[str, Any]]:
    """Writes ``mask`` (resized to the photo) where the engine would write its mask."""

    def cut_out(image: Path, workdir: Path, **_: Any) -> dict[str, Any]:
        calls.append(image)
        with Image.open(image) as opened:
            size = opened.size
        workdir.mkdir(parents=True, exist_ok=True)
        Image.fromarray(_alpha(mask)).resize(size).save(workdir / "mask.png")
        return {"ok": True, "background_removed": True, "mask": str(workdir / "mask.png")}

    return cut_out


@pytest.fixture
def engine_with(monkeypatch: pytest.MonkeyPatch) -> Callable[[np.ndarray], list[Path]]:
    """The engine counts as installed and cuts out ``mask``. Returns the calls."""

    def install(mask: np.ndarray) -> list[Path]:
        calls: list[Path] = []
        monkeypatch.setattr(engine, "check_local_ai", lambda: None)
        monkeypatch.setattr(engine, "cut_out", _stand_in(calls, mask))
        return calls

    return install


def _capture(client: TestClient, pid: str) -> dict[str, Any]:
    return dict(client.get(f"/api/v1/projects/{pid}/capture-assessment").json())


def _cutout_check(assessment: dict[str, Any]) -> dict[str, Any] | None:
    return next((c for c in assessment["checks"] if c["code"] == "cutout"), None)


def test_the_cut_out_check_runs_on_request_and_is_stored(
    client: TestClient,
    make_project: MakeProject,
    upload: Upload,
    engine_with: Callable[[np.ndarray], list[Path]],
) -> None:
    picture, mask = images.vase(centre=(0.5, 0.8))
    calls = engine_with(mask)
    pid = make_project(input_mode="single_image")["id"]
    image = upload(pid, ("vase.jpg", images.encode(picture, "JPEG", quality=92), "image/jpeg"))
    image_id = image["results"][0]["image"]["id"]
    assert image["results"][0]["image"]["cutout_url"] is None

    before = _capture(client, pid)
    check = _cutout_check(before)
    assert check is not None
    assert (check["status"], check["action"]) == ("not_assessed", "check_cutout")
    assert calls == []  # nothing runs until asked

    response = client.post(f"/api/v1/projects/{pid}/capture-assessment/cutout")
    assert response.status_code == 200, response.text
    check = _cutout_check(response.json())
    assert check is not None
    assert check["status"] == "warning"
    assert check["title"] == "Object runs off the photo"
    assert "bottom edge" in check["message"]
    assert check["image_ids"] == [image_id]
    assert response.json()["readiness"] == "needs_attention"
    assert len(calls) == 1

    read = client.get(f"/api/v1/images/{image_id}").json()
    found = read["analysis"]["cutout"]
    assert found["touches"] == ["bottom"]
    assert found["background_removed"] is True
    assert _codes(found["warnings"]) == {"subject_cropped"}
    preview = client.get(read["cutout_url"])
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(preview.content)).size == (512, 512)

    # Asking again does not re-run a photo that already has a result.
    assert client.post(f"/api/v1/projects/{pid}/capture-assessment/cutout").status_code == 200
    assert len(calls) == 1

    # The preview goes with the photo.
    assert any(k.endswith(f"{image_id}/cutout.png") for k in stored_keys(client))
    assert client.delete(f"/api/v1/images/{image_id}").status_code == 204
    assert not any(k.endswith(f"{image_id}/cutout.png") for k in stored_keys(client))


def test_a_clean_cut_out_passes(
    client: TestClient,
    make_project: MakeProject,
    upload: Upload,
    engine_with: Callable[[np.ndarray], list[Path]],
) -> None:
    picture, mask = images.vase()
    engine_with(mask)
    pid = make_project(input_mode="single_image")["id"]
    upload(pid, ("vase.jpg", images.encode(picture, "JPEG", quality=92), "image/jpeg"))
    response = client.post(f"/api/v1/projects/{pid}/capture-assessment/cutout")
    check = _cutout_check(response.json())
    assert check is not None
    assert (check["status"], check["title"]) == ("pass", "Clean cut-out")


@pytest.mark.usefixtures("no_local_ai")
def test_without_the_engine_only_photos_with_transparency_are_checked(
    client: TestClient, make_project: MakeProject, upload: Upload
) -> None:
    picture, _ = images.vase()
    pid = make_project(input_mode="single_image")["id"]
    upload(pid, ("vase.jpg", images.encode(picture, "JPEG", quality=92), "image/jpeg"))
    assert _cutout_check(_capture(client, pid)) is None  # not offered, and not ready-blocking
    assert _capture(client, pid)["readiness"] == "ready"
    response = client.post(f"/api/v1/projects/{pid}/capture-assessment/cutout")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "local_ai_not_installed"

    # A cut-out PNG needs no engine: its own transparency is the cut-out.
    _, mask = images.vase()
    pid = make_project(input_mode="single_image")["id"]
    result = upload(pid, ("vase.png", _png_with_alpha(mask), "image/png"))
    image_id = result["results"][0]["image"]["id"]
    response = client.post(f"/api/v1/projects/{pid}/capture-assessment/cutout")
    assert response.status_code == 200, response.text
    check = _cutout_check(response.json())
    assert check is not None
    assert check["status"] == "pass"
    found = client.get(f"/api/v1/images/{image_id}").json()["analysis"]["cutout"]
    assert found["background_removed"] is False
    assert found["background_match"] is None


def test_the_cut_out_is_only_for_single_image_projects(
    client: TestClient, make_project: MakeProject, upload: Upload
) -> None:
    pid = make_project(input_mode="multi_view")["id"]
    response = client.post(f"/api/v1/projects/{pid}/capture-assessment/cutout")
    assert response.status_code == 422
    assert client.get(f"/api/v1/images/{pid}/cutout").status_code == 404


# -- the installed engine ----------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(
    not REAL_ENGINE or not engine.status(Path(REAL_ENGINE)).installed,
    reason="set AI3D_LOCAL_AI_DIR to an installed engine (ai3d-local-ai install)",
)
@pytest.mark.parametrize(
    ("background", "colour", "centre", "expected"),
    [
        ((215, 215, 210), (150, 70, 40), (0.5, 0.5), set()),
        ((215, 215, 210), (150, 70, 40), (0.5, 0.85), {"subject_cropped"}),
        ((18, 16, 18), (40, 36, 40), (0.5, 0.5), {"low_edge_contrast"}),
        ((235, 235, 235), (250, 250, 250), (0.5, 0.5), {"low_edge_contrast"}),
    ],
    ids=["clean", "cropped", "dark-on-dark", "white-on-white"],
)
def test_the_real_engine_cut_out_gives_the_expected_warnings(
    tmp_path: Path,
    background: tuple[int, int, int],
    colour: tuple[int, int, int],
    centre: tuple[float, float],
    expected: set[str],
) -> None:
    engine.configure(Path(str(REAL_ENGINE)))
    picture, truth = images.vase(background, colour, size=(1200, 900), centre=centre)
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(images.encode(picture, "JPEG", quality=92))
    result = engine.cut_out(photo, tmp_path / "work")
    assert result["background_removed"] is True
    alpha = np.asarray(Image.open(result["mask"]))
    assert alpha.shape == truth.shape
    found = alpha > cutout.MASK_THRESHOLD
    assert (found & truth).sum() / (found | truth).sum() > 0.8  # the mask finds the vase
    assessment = cutout.assess(np.asarray(picture), alpha, background_removed=True)
    assert _codes(assessment.warnings) == expected
