"""Provider photo copies and generated-mesh validation."""

import io
import json
import struct
from pathlib import Path
from typing import cast

import numpy as np
import pytest
import trimesh
from PIL import Image, ImageCms

from ai3d.models.enums import ReportStatus
from ai3d.processing.mesh import validate_mesh_file
from ai3d.processing.preprocess import preprocess_for_provider
from tests import images

LIMITS = {"max_pixels": 100_000_000, "max_side": 1024, "max_bytes": 20 * 1024 * 1024}


def _write(tmp_path: Path, data: bytes, name: str = "in") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_copy_is_upright_resized_and_metadata_free(tmp_path: Path) -> None:
    source = images.jpeg_with_exif(images.textured(2400, 1600), orientation=6, gps=True)
    result = preprocess_for_provider(_write(tmp_path, source), **LIMITS)
    assert result.mime_type == "image/jpeg"
    assert (result.width, result.height) == (683, 1024)
    assert result.resized is True
    with Image.open(io.BytesIO(result.data)) as img:
        assert img.size == (683, 1024)
        assert not img.getexif()
        assert "exif" not in img.info
    assert b"TestCam" not in result.data


def test_small_photo_is_not_upscaled(tmp_path: Path) -> None:
    source = images.encode(images.textured(640, 480), "JPEG")
    result = preprocess_for_provider(_write(tmp_path, source), **LIMITS)
    assert (result.width, result.height) == (640, 480)
    assert result.resized is False


def test_transparent_cutout_stays_png(tmp_path: Path) -> None:
    rgba = images.textured(600, 400).convert("RGBA")
    alpha = Image.new("L", rgba.size, 0)
    alpha.paste(255, (100, 100, 500, 300))
    rgba.putalpha(alpha)
    result = preprocess_for_provider(_write(tmp_path, images.encode(rgba, "PNG")), **LIMITS)
    assert result.mime_type == "image/png"
    with Image.open(io.BytesIO(result.data)) as img:
        assert img.mode == "RGBA"
        assert img.getchannel("A").getextrema() == (0, 255)


def test_opaque_png_becomes_jpeg(tmp_path: Path) -> None:
    opaque = images.textured(600, 400).convert("RGBA")
    result = preprocess_for_provider(_write(tmp_path, images.encode(opaque, "PNG")), **LIMITS)
    assert result.mime_type == "image/jpeg"


def test_icc_profile_is_converted_and_removed(tmp_path: Path) -> None:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB"))
    buffer = io.BytesIO()
    images.textured(300, 200).save(buffer, format="JPEG", icc_profile=profile.tobytes())
    result = preprocess_for_provider(_write(tmp_path, buffer.getvalue()), **LIMITS)
    with Image.open(io.BytesIO(result.data)) as img:
        assert "icc_profile" not in img.info


def test_size_limit_is_enforced(tmp_path: Path) -> None:
    source = images.encode(images.textured(800, 600), "JPEG")
    with pytest.raises(ValueError, match="provider limit"):
        preprocess_for_provider(_write(tmp_path, source), **{**LIMITS, "max_bytes": 1000})


# -- mesh validation ------------------------------------------------------------------


def _glb(tmp_path: Path, scene: trimesh.Scene | trimesh.Trimesh) -> Path:
    path = tmp_path / "model.glb"
    data = cast(bytes, scene.export(file_type="glb"))
    path.write_bytes(data)
    return path


def test_closed_mesh_passes(tmp_path: Path) -> None:
    report = validate_mesh_file(_glb(tmp_path, trimesh.creation.box()))
    assert report.status == ReportStatus.PASS
    assert report.metrics["triangle_count"] == 12
    assert report.metrics["watertight"] is True
    assert report.metrics["components"] == 1
    assert report.metrics["bbox_size"] == [1.0, 1.0, 1.0]


def test_open_multi_part_mesh_is_described(tmp_path: Path) -> None:
    box = trimesh.creation.box()
    box.update_faces(np.arange(len(box.faces)) != 0)  # punch a hole
    other = trimesh.creation.icosphere(subdivisions=1)
    other.apply_translation([3, 0, 0])
    report = validate_mesh_file(_glb(tmp_path, trimesh.Scene([box, other])))
    codes = {i.code for i in report.issues}
    assert {"not_watertight", "multiple_components"} <= codes
    assert report.metrics["mesh_count"] == 2
    assert report.usable


def test_degenerate_triangles_warn(tmp_path: Path) -> None:
    vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [2, 0, 0]], dtype=float)
    faces = np.array([[0, 1, 2], [0, 1, 3]])  # second triangle is collinear
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    report = validate_mesh_file(_glb(tmp_path, mesh))
    assert report.status == ReportStatus.WARN
    assert report.metrics["degenerate_triangles"] == 1


@pytest.mark.parametrize("data", [b"", b"glTF garbage", b"\x00" * 64])
def test_unreadable_files_fail(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "bad.glb"
    path.write_bytes(data)
    report = validate_mesh_file(path)
    assert report.status == ReportStatus.FAIL
    assert not report.usable


def test_model_without_meshes_fails(tmp_path: Path) -> None:
    # A valid GLB container whose scene has no meshes at all.
    doc = json.dumps({"asset": {"version": "2.0"}, "scenes": [{"nodes": []}], "scene": 0})
    chunk = doc.encode().ljust((len(doc) + 3) // 4 * 4, b" ")
    header = struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(chunk))
    path = tmp_path / "empty.glb"
    path.write_bytes(header + struct.pack("<I4s", len(chunk), b"JSON") + chunk)
    report = validate_mesh_file(path)
    assert report.status == ReportStatus.FAIL
    assert report.issues[0].code in {"no_geometry", "unreadable"}


def test_texture_seams_are_not_counted_as_holes_or_pieces(tmp_path: Path) -> None:
    """Textured files split vertices along UV seams; the shape is still one closed box."""
    box = trimesh.creation.box(extents=(1.0, 0.5, 0.25))
    box.unmerge_vertices()  # every face gets its own vertices, as with per-face UVs
    rng = np.random.default_rng(0)
    texture = Image.fromarray(rng.integers(0, 255, (16, 16, 3), dtype=np.uint8))
    box.visual = trimesh.visual.TextureVisuals(uv=rng.random((len(box.vertices), 2)), image=texture)
    path = tmp_path / "box.glb"
    path.write_bytes(trimesh.Scene(box).export(file_type="glb"))

    report = validate_mesh_file(path)
    assert report.metrics["vertex_count"] == 36  # as stored: split along the seams
    assert report.metrics["texture_count"] == 1
    assert report.metrics["components"] == 1
    assert report.metrics["watertight"] is True
    assert report.status == ReportStatus.PASS


def test_stray_triangles_far_from_the_surface_warn(tmp_path: Path) -> None:
    """Like sparse photogrammetry on real photos: a compact shape plus spikes to far points."""
    body = trimesh.creation.icosphere(subdivisions=3, radius=0.5)
    spikes = [
        trimesh.Trimesh(vertices=[[0, 0, 0.5], [0.05, 0, 0.5], [x, y, 20.0]], faces=[[0, 1, 2]])
        for x, y in ((1, 0), (-1, 0), (0, 1))
    ]
    path = tmp_path / "spiky.glb"
    path.write_bytes(trimesh.Scene([body, *spikes]).export(file_type="glb"))
    report = validate_mesh_file(path)
    assert report.metrics["spread_ratio"] > 4
    issue = next(i for i in report.issues if i.code == "stray_surface")
    assert issue.severity == "warning"
    assert report.status == ReportStatus.WARN

    compact = tmp_path / "compact.glb"
    compact.write_bytes(trimesh.Scene(body).export(file_type="glb"))
    assert validate_mesh_file(compact).metrics["spread_ratio"] < 2


def test_a_shattered_surface_warns(tmp_path: Path) -> None:
    rng = np.random.default_rng(1)
    pieces = [
        trimesh.creation.icosphere(subdivisions=1, radius=0.05).apply_translation(rng.random(3))
        for _ in range(40)
    ]
    path = tmp_path / "shattered.glb"
    path.write_bytes(trimesh.Scene(pieces).export(file_type="glb"))
    report = validate_mesh_file(path)
    assert report.metrics["components"] == 40
    assert report.metrics["largest_component_share"] == pytest.approx(1 / 40, rel=0.01)
    assert any(i.code == "fragmented" and i.severity == "warning" for i in report.issues)
