"""Per-vertex colours and printing axes in every export format.

Local AI generation produces meshes with per-vertex colours instead of a
texture. These tests use an asymmetric, colour-graded box so that a lost
colour, a dark (linear) OBJ colour or a lying-down STL/3MF all show up.
"""

import importlib.util
import io
import zipfile
from pathlib import Path

import numpy as np
import pytest
import trimesh

from ai3d.models.enums import ExportFormat
from ai3d.processing import exporters
from ai3d_engine.blender import check_blender

USD = importlib.util.find_spec("pxr") is not None
BLENDER = check_blender() is None


def _coloured_box() -> trimesh.Scene:
    """0.2 × 0.5 × 0.1 m (glTF axes, Y up), red at the bottom → blue at the top (linear)."""
    box = trimesh.creation.box(extents=(0.2, 0.5, 0.1))
    box = box.subdivide().subdivide()
    height = (box.vertices[:, 1] - box.vertices[:, 1].min()) / np.ptp(box.vertices[:, 1])
    colours = np.zeros((len(box.vertices), 4), dtype=np.uint8)
    colours[:, 0] = np.round((1 - height) * 200).astype(np.uint8)
    colours[:, 2] = np.round(height * 200).astype(np.uint8)
    colours[:, 3] = 255
    box.visual = trimesh.visual.ColorVisuals(box, vertex_colors=colours)
    return trimesh.Scene(box)


def _available(fmt: ExportFormat) -> bool:
    return exporters.format_info(fmt).available


@pytest.mark.parametrize("fmt", [f for f in ExportFormat if f != ExportFormat.STL])
def test_colours_survive_every_format_that_has_them(tmp_path: Path, fmt: ExportFormat) -> None:
    if not _available(fmt):
        pytest.skip(f"{fmt.value} export is not available here")
    scene = _coloured_box()
    facts = exporters.scene_facts(scene)
    assert facts.coloured
    out = tmp_path / f"box.{fmt.value}"
    exporters.write(scene, fmt, out)
    result = exporters.validate(out, fmt, facts)
    assert result["coloured"] is True
    assert exporters.format_info(fmt).keeps_colour


def test_stl_says_it_has_no_colour(tmp_path: Path) -> None:
    info = exporters.format_info(ExportFormat.STL)
    assert not info.keeps_colour
    assert "no colour" in info.notes[0]
    out = tmp_path / "box.stl"
    exporters.write(_coloured_box(), ExportFormat.STL, out)
    assert (
        exporters.validate(out, ExportFormat.STL, exporters.scene_facts(_coloured_box()))[
            "coloured"
        ]
        is False
    )


@pytest.mark.parametrize("fmt", [ExportFormat.STL, ExportFormat.THREE_MF])
def test_printing_formats_are_millimetres_with_z_up(tmp_path: Path, fmt: ExportFormat) -> None:
    out = tmp_path / f"box.{fmt.value}"
    exporters.write(_coloured_box(), fmt, out)
    mesh = trimesh.load(out, file_type=fmt.value, force="mesh")
    # The 0.5 m height (glTF Y) is now Z, in millimetres; slicers put Z up.
    assert mesh.extents == pytest.approx([200, 100, 500], rel=1e-4)
    result = exporters.validate(out, fmt, exporters.scene_facts(_coloured_box()))
    assert result["axes"] == "Z up"
    assert result["units"] == "millimetres"


def test_obj_colours_are_written_as_srgb(tmp_path: Path) -> None:
    out = tmp_path / "box.obj"
    exporters.write(_coloured_box(), ExportFormat.OBJ, out)
    text = zipfile.ZipFile(out).read("model.obj").decode()
    values = [list(map(float, line.split()[4:7])) for line in text.splitlines() if line[:2] == "v "]
    reds = max(v[0] for v in values)
    # Linear 200/255 = 0.784 is sRGB 0.900; writing it unconverted would look too dark.
    assert reds == pytest.approx(0.9, abs=0.01)


def test_3mf_colours_are_a_colour_group(tmp_path: Path) -> None:
    out = tmp_path / "box.3mf"
    exporters.write(_coloured_box(), ExportFormat.THREE_MF, out)
    model = zipfile.ZipFile(out).read("3D/3dmodel.model").decode()
    assert "<m:colorgroup" in model
    assert 'p1="' in model
    assert 'unit="millimeter"' in model
    # The geometry is a normal 3MF that an independent reader opens.
    mesh = trimesh.load(io.BytesIO(out.read_bytes()), file_type="3mf", force="mesh")
    assert len(mesh.faces) == len(_coloured_box().to_mesh().faces)


@pytest.mark.skipif(not USD, reason="needs Pixar USD (the usdz extra)")
def test_usdz_colours_feed_the_material(tmp_path: Path) -> None:
    from pxr import Usd, UsdGeom  # noqa: PLC0415 - optional

    out = tmp_path / "box.usdz"
    exporters.write(_coloured_box(), ExportFormat.USDZ, out)
    stage = Usd.Stage.Open(str(out))
    mesh = next(p for p in stage.Traverse() if p.IsA(UsdGeom.Mesh))
    display = UsdGeom.PrimvarsAPI(mesh).GetPrimvar("displayColor")
    assert display.GetInterpolation() == UsdGeom.Tokens.vertex
    reader = next(p for p in stage.Traverse() if p.GetName() == "ColourReader")
    assert reader.GetAttribute("inputs:varname").Get() == "displayColor"


def test_losing_the_colours_is_caught(tmp_path: Path) -> None:
    scene = _coloured_box()
    facts = exporters.scene_facts(scene)
    plain = scene.copy()
    for geometry in plain.geometry.values():
        geometry.visual = trimesh.visual.ColorVisuals(geometry)  # colours dropped
    out = tmp_path / "box.glb"
    out.write_bytes(plain.export(file_type="glb"))
    with pytest.raises(exporters.ExportError, match="colours are missing"):
        exporters.validate(out, ExportFormat.GLB, facts)


@pytest.mark.skipif(not BLENDER, reason="needs Blender 4.2+")
def test_fbx_keeps_the_colours_according_to_blender(tmp_path: Path) -> None:
    out = tmp_path / "box.fbx"
    exporters.write(_coloured_box(), ExportFormat.FBX, out)
    found = exporters.read_back(out, ExportFormat.FBX)
    assert found.coloured


# -- files from other tools ---------------------------------------------------------------


def test_draco_compressed_models_are_decoded_and_checked(tmp_path: Path) -> None:
    from ai3d.processing.mesh import validate_mesh_file  # noqa: PLC0415

    scene = _coloured_box()
    path = tmp_path / "draco.glb"
    path.write_bytes(scene.export(file_type="glb", extension_draco=True))
    assert b"KHR_draco_mesh_compression" in path.read_bytes()[:4096]
    report = validate_mesh_file(path)
    assert report.usable
    assert report.metrics["triangle_count"] == len(scene.to_mesh().faces)


@pytest.mark.skipif(not BLENDER, reason="needs Blender 4.2+ (an independent Draco encoder)")
def test_a_blender_draco_file_with_a_material_and_colours_exports_everywhere(
    tmp_path: Path,
) -> None:
    """Blender writes COLOR_0 as float RGB next to a material, Draco-compressed.

    trimesh then keeps the colours as a side attribute that its own glTF writer
    could not export; the loader normalises that.
    """
    from ai3d.processing.mesh import load_model_scene, validate_mesh_file  # noqa: PLC0415
    from ai3d_engine.blender import find_blender  # noqa: PLC0415
    from ai3d_engine.runtime import run_tool  # noqa: PLC0415

    source = tmp_path / "source.glb"
    source.write_bytes(_coloured_box().export(file_type="glb"))
    target = tmp_path / "blender-draco.glb"
    script = (
        "import bpy, sys\n"
        "bpy.ops.wm.read_factory_settings(use_empty=True)\n"
        f"bpy.ops.import_scene.gltf(filepath={str(source)!r})\n"
        f"bpy.ops.export_scene.gltf(filepath={str(target)!r}, export_format='GLB', "
        "export_draco_mesh_compression_enable=True)\n"
    )
    assert find_blender() is not None
    run_tool("blender", ["--background", "--factory-startup", "--python-expr", script], timeout=300)
    assert target.is_file()
    assert validate_mesh_file(target).usable
    scene = load_model_scene(target)
    facts = exporters.scene_facts(scene)
    assert facts.coloured
    out = tmp_path / "again.glb"
    exporters.write(scene, ExportFormat.GLB, out)  # crashed before the colours were normalised
    assert exporters.validate(out, ExportFormat.GLB, facts)["coloured"]
