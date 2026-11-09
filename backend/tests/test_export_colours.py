"""Per-vertex colours and printing axes in every export format.

Local AI generation produces meshes with per-vertex colours instead of a
texture. These tests use an asymmetric, colour-graded box so that a lost
colour, a dark (linear) OBJ colour or a lying-down STL/3MF all show up.
"""

import importlib.util
import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
import trimesh
from PIL import Image

from ai3d.models.enums import ExportFormat
from ai3d.processing import exporters
from ai3d.processing.mesh import load_model_scene, validate_mesh_file
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


def test_a_textured_model_keeps_its_colours_in_3mf(tmp_path: Path) -> None:
    """3MF has no textures: the texture's colour at each vertex becomes a colour group
    (refined local AI models are textured, and 3MF is for colour printing)."""
    box = trimesh.creation.box(extents=(0.2, 0.5, 0.1)).subdivide().subdivide()
    box.unmerge_vertices()
    height = (box.vertices[:, 1] - box.vertices[:, 1].min()) / np.ptp(box.vertices[:, 1])
    image = np.zeros((64, 64, 3), np.uint8)
    image[:32] = (20, 40, 220)  # top half of the texture: blue (sRGB)
    image[32:] = (220, 30, 20)  # bottom half: red
    uv = np.stack([np.full(len(height), 0.5), np.where(height > 0.5, 0.9, 0.1)], axis=1)
    material = trimesh.visual.material.PBRMaterial(baseColorTexture=Image.fromarray(image))
    box.visual = trimesh.visual.TextureVisuals(uv=uv, material=material)
    scene = trimesh.Scene(box)
    facts = exporters.scene_facts(scene)
    assert facts.textured
    assert not facts.coloured
    out = tmp_path / "box.3mf"
    exporters.write(scene, ExportFormat.THREE_MF, out)
    assert exporters.validate(out, ExportFormat.THREE_MF, facts)["coloured"] is True
    model = zipfile.ZipFile(out).read("3D/3dmodel.model").decode()
    # Written as the texture's own sRGB colours, top blue and bottom red.
    assert 'color="#1428DC"' in model
    assert 'color="#DC1E14"' in model
    assert "colour group. A texture becomes per-vertex colours" in " ".join(
        exporters.format_info(ExportFormat.THREE_MF).notes
    )


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


@pytest.mark.parametrize("fmt", [ExportFormat.STL, ExportFormat.THREE_MF])
def test_a_mirrored_part_exports_with_outward_faces(tmp_path: Path, fmt: ExportFormat) -> None:
    """The workspace stores a mirror as a negative scale; exports must bake it correctly."""
    box = trimesh.creation.box(extents=(0.2, 0.5, 0.1))
    box.apply_translation([0.3, 0, 0])
    scene = trimesh.Scene()
    scene.add_geometry(box, node_name="part", transform=np.diag([-1.0, 1.0, 1.0, 1.0]))
    source = tmp_path / "mirrored.glb"
    source.write_bytes(scene.export(file_type="glb"))
    assert validate_mesh_file(source).metrics["inside_out"] is False
    loaded = load_model_scene(source)
    out = tmp_path / f"mirrored.{fmt.value}"
    exporters.write(loaded, fmt, out)
    mesh = trimesh.load(out, file_type=fmt.value, force="mesh")
    assert mesh.is_watertight
    assert mesh.volume > 0  # faces point outwards
    assert mesh.bounds[:, 0].mean() < 0  # and the part really moved to -X


def _widen_colours(glb: bytes) -> bytes:
    """The same GLB with COLOR_0 stored as normalised 16-bit values (as Blender writes)."""
    length = int.from_bytes(glb[12:16], "little")
    doc = json.loads(glb[20 : 20 + length])
    bin_at = 20 + length
    binary = bytearray(
        glb[bin_at + 8 : bin_at + 8 + int.from_bytes(glb[bin_at : bin_at + 4], "little")]
    )
    accessor = doc["accessors"][doc["meshes"][0]["primitives"][0]["attributes"]["COLOR_0"]]
    view = doc["bufferViews"][accessor["bufferView"]]
    start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    values = np.frombuffer(bytes(binary), np.uint8, accessor["count"] * 4, start)
    # v * 257 + 100 still scales back to v, but wraps to (v + 100) % 256 if truncated.
    wide = values.astype(np.uint16) * 257 + 100
    binary.extend(b"\x00" * (-len(binary) % 4))
    doc["bufferViews"].append({"buffer": 0, "byteOffset": len(binary), "byteLength": wide.nbytes})
    binary.extend(wide.tobytes())
    accessor.update(bufferView=len(doc["bufferViews"]) - 1, byteOffset=0, componentType=5123)
    doc["buffers"][0]["byteLength"] = len(binary)
    text = json.dumps(doc).encode()
    text += b" " * (-len(text) % 4)
    total = 28 + len(text) + len(binary)
    return (
        b"glTF"
        + (2).to_bytes(4, "little")
        + total.to_bytes(4, "little")
        + len(text).to_bytes(4, "little")
        + b"JSON"
        + text
        + len(binary).to_bytes(4, "little")
        + b"BIN\x00"
        + bytes(binary)
    )


def test_sixteen_bit_vertex_colours_are_read_correctly(tmp_path: Path) -> None:
    scene = _coloured_box()
    expected = np.asarray(next(iter(scene.geometry.values())).visual.vertex_colors)[:, :3]
    path = tmp_path / "wide.glb"
    path.write_bytes(_widen_colours(scene.export(file_type="glb")))
    plain = trimesh.load(path, file_type="glb", force="mesh")
    assert not np.array_equal(np.asarray(plain.visual.vertex_colors)[:, :3], expected)  # trimesh
    (mesh,) = load_model_scene(path).geometry.values()
    colours = np.asarray(mesh.visual.vertex_colors)[:, :3].astype(int)
    assert np.abs(colours - expected.astype(int)).max() <= 1
    assert validate_mesh_file(path).status != "fail"


def test_colours_next_to_a_texture_survive_saving_again(tmp_path: Path) -> None:
    """Enhance re-saves models through trimesh; colours used to come back black."""
    box = trimesh.creation.box(extents=(0.2, 0.5, 0.1))
    rng = np.random.default_rng(4)
    colours = rng.integers(0, 256, (len(box.vertices), 4), dtype=np.uint8)
    colours[:, 3] = 255
    box.visual = trimesh.visual.TextureVisuals(
        uv=rng.random((len(box.vertices), 2)),
        material=trimesh.visual.material.PBRMaterial(
            normalTexture=Image.fromarray(np.full((8, 8, 3), 128, np.uint8))
        ),
    )
    box.visual.vertex_attributes["color"] = colours
    first = tmp_path / "first.glb"
    first.write_bytes(trimesh.Scene(box).export(file_type="glb"))
    second = tmp_path / "second.glb"
    second.write_bytes(load_model_scene(first).export(file_type="glb"))
    (mesh,) = load_model_scene(second).geometry.values()
    assert mesh.visual.material.normalTexture is not None
    again = np.asarray(mesh.visual.vertex_attributes["color"]).astype(int)
    assert np.abs(again - colours.astype(int)).max() <= 1
