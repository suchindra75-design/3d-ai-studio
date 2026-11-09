"""Exporting a version to other formats — and proving each file by re-opening it.

Every export is read back with an independent reader (trimesh; Pixar USD for
USDZ; for FBX, Blender's FBX importer in a separate headless run) and compared
with the source: same triangle count, same size (after the unit conversion the
format implies), texture present when the format carries one. A file that fails
is never offered for download.

Units and axes: glTF is in metres with Y up. STL and 3MF are read by slicers
and CAD tools as millimetres with Z up, so they are written that way (rotated
+90° about X). When the model's real scale is unknown the numbers are still
the model's own units — the export says so. FBX files declare the metre as
their unit and are Y-up, like glTF.

Colours: per-vertex colours (e.g. from local AI generation) are kept by GLB,
glTF, OBJ (sRGB values on each vertex line), 3MF (a colour group), USDZ
(displayColor) and FBX, and checked on re-opening like textures.
"""

import importlib.util
import io
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import numpy as np
import trimesh
from PIL import Image
from trimesh.exchange.obj import export_obj

from ai3d.models.enums import ExportFormat
from ai3d.processing import quads
from ai3d.processing.mesh import normalise_vertex_colours
from ai3d_engine.blender import (
    BlenderFailedError,
    BlenderNotFoundError,
    check_blender,
    run_blender,
)

SIZE_TOLERANCE = 1e-3  # relative
BLENDER_TIMEOUT = 900.0  # seconds, per Blender run (FBX export or re-opening)
_FBX_MAGIC = b"Kaydara FBX Binary  \x00"


class ExportError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class FormatInfo:
    format: ExportFormat
    label: str
    extension: str
    media_type: str
    available: bool
    reason: str | None
    keeps_texture: bool
    notes: tuple[str, ...] = ()
    keeps_colour: bool = False  # per-vertex colours


def _usd_available() -> bool:
    return importlib.util.find_spec("pxr") is not None


def formats() -> list[FormatInfo]:
    usd = _usd_available()
    blender_problem = check_blender()
    return [
        FormatInfo(
            ExportFormat.GLB,
            "GLB",
            "glb",
            "model/gltf-binary",
            True,
            None,
            True,
            ("Single file with textures; the most widely supported format.",),
            keeps_colour=True,
        ),
        FormatInfo(
            ExportFormat.GLTF,
            "glTF (.zip)",
            "zip",
            "application/zip",
            True,
            None,
            True,
            ("model.gltf with its .bin buffers and textures, zipped.",),
            keeps_colour=True,
        ),
        FormatInfo(
            ExportFormat.OBJ,
            "OBJ + MTL (.zip)",
            "zip",
            "application/zip",
            True,
            None,
            True,
            (
                "model.obj, model.mtl and texture images, zipped. PBR values beyond "
                "the base colour are not part of OBJ.",
                "Per-vertex colours are written as sRGB values on each vertex line (read by "
                "Blender, MeshLab and most tools).",
            ),
            keeps_colour=True,
        ),
        FormatInfo(
            ExportFormat.STL,
            "STL",
            "stl",
            "model/stl",
            True,
            None,
            False,
            ("Geometry only: no colour or texture. Millimetres, Z up (as slicers expect).",),
        ),
        FormatInfo(
            ExportFormat.THREE_MF,
            "3MF",
            "3mf",
            "model/3mf",
            True,
            None,
            False,
            (
                "For 3D printing: millimetres, Z up. Per-vertex colours are included as a 3MF "
                "colour group. A texture becomes per-vertex colours (its colour at each "
                "corner), so detail finer than the triangles is lost.",
            ),
            keeps_colour=True,
        ),
        FormatInfo(
            ExportFormat.USDZ,
            "USDZ",
            "usdz",
            "model/vnd.usdz+zip",
            usd,
            None if usd else "Needs Pixar USD (the usdz extra: usd-core).",
            True,
            ("For Apple AR Quick Look; base-colour texture or per-vertex colours.",),
            keeps_colour=True,
        ),
        FormatInfo(
            ExportFormat.FBX,
            "FBX",
            "fbx",
            "application/octet-stream",
            blender_problem is None,
            blender_problem,
            True,
            (
                "Written by Blender with the textures embedded. Y-up; the file's unit is the "
                "metre. Only the base colour is carried as a texture; per-vertex colours "
                "are kept.",
            ),
            keeps_colour=True,
        ),
    ]


def format_info(fmt: ExportFormat) -> FormatInfo:
    return next(info for info in formats() if info.format == fmt)


# -- facts used for validation ---------------------------------------------------------


@dataclass(slots=True)
class Facts:
    triangles: int
    size: list[float]
    textured: bool
    extra: dict[str, Any] = field(default_factory=dict)
    coloured: bool = False  # per-vertex colours that actually vary


def _vertex_colours(mesh: trimesh.Trimesh) -> np.ndarray | None:
    """RGBA uint8 per vertex, or None when the mesh has no per-vertex colours."""
    visual = mesh.visual
    if visual is None or visual.kind != "vertex":
        return None
    return np.asarray(getattr(visual, "vertex_colors", None))


def _has_colours(mesh: trimesh.Trimesh) -> bool:
    colours = _vertex_colours(mesh)
    return colours is not None and len(colours) > 0 and float(colours[:, :3].std()) > 0.5


def scene_facts(scene: trimesh.Scene) -> Facts:
    meshes = [g for g in scene.dump() if isinstance(g, trimesh.Trimesh)]
    textured_meshes = sum(
        1
        for m in meshes
        if m.visual is not None and m.visual.kind == "texture" and _base_image(m) is not None
    )
    return Facts(
        triangles=sum(len(m.faces) for m in meshes),
        size=[float(v) for v in scene.extents] if meshes else [0.0, 0.0, 0.0],
        textured=textured_meshes > 0,
        extra={"textured_meshes": textured_meshes},
        coloured=any(_has_colours(m) for m in meshes),
    )


def _base_image(mesh: trimesh.Trimesh) -> Image.Image | None:
    material = getattr(mesh.visual, "material", None)
    for attr in ("baseColorTexture", "image"):
        image = getattr(material, attr, None) if material is not None else None
        if isinstance(image, Image.Image):
            return image
    return None


# -- writers ---------------------------------------------------------------------------


# Characters of a large text file encoded and written at a time.
_TEXT_PIECE = 16 * 1024 * 1024


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    return buffer.getvalue()


def _as_bytes(data: object) -> bytes:
    if not isinstance(data, bytes):
        raise ExportError("The exporter did not produce a binary file.")
    return data


# glTF (Y up) → printing/CAD convention (Z up): +90° about X, so the front (+Z) faces -Y.
_Y_UP_TO_Z_UP = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], dtype=float)


def _for_printing(scene: trimesh.Scene) -> trimesh.Trimesh:
    """One mesh in millimetres with Z up, as slicers and CAD tools read STL and 3MF."""
    mesh: trimesh.Trimesh = scene.to_mesh()  # all instances in world coordinates, one mesh
    mesh.apply_transform(_Y_UP_TO_Z_UP)
    mesh.apply_scale(1000.0)  # type: ignore[no-untyped-call]
    return mesh


def _linear_to_srgb(values: np.ndarray) -> np.ndarray:
    """glTF vertex colours are linear; OBJ, 3MF and most tools expect sRGB."""
    linear = np.clip(values.astype(np.float64) / 255.0, 0.0, 1.0)
    srgb = np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)
    return np.round(srgb * 255.0).astype(np.uint8)


def _srgb_colour_scene(scene: trimesh.Scene) -> trimesh.Scene:
    """A copy whose per-vertex colours are sRGB (for formats that store sRGB)."""
    copy = scene.copy()
    for geometry in copy.geometry.values():
        colours = _vertex_colours(geometry) if isinstance(geometry, trimesh.Trimesh) else None
        if colours is not None and _has_colours(geometry):
            colours = colours.copy()
            colours[:, :3] = _linear_to_srgb(colours[:, :3])
            geometry.visual = trimesh.visual.ColorVisuals(geometry, vertex_colors=colours)
    return copy


def _srgb_colours_with_textures(scene: trimesh.Scene) -> trimesh.Scene:
    """A copy whose per-vertex colours are sRGB, in which textured meshes without
    per-vertex colours get their base-colour texture's colour at each vertex, for
    formats that keep colours but not textures (3MF). Kept in sRGB throughout: 8-bit
    linear values would round dark colours coarsely."""
    copy = _srgb_colour_scene(scene)
    for geometry in copy.geometry.values():
        if not isinstance(geometry, trimesh.Trimesh) or _vertex_colours(geometry) is not None:
            continue
        image = _base_image(geometry)
        uv = getattr(geometry.visual, "uv", None)
        if image is None or uv is None or len(uv) != len(geometry.vertices):
            continue
        pixels = np.asarray(image.convert("RGB"))
        height, width = pixels.shape[:2]
        uv = np.asarray(uv, dtype=np.float64)
        x = np.clip(np.round((uv[:, 0] % 1.0) * (width - 1)).astype(np.int64), 0, width - 1)
        y = np.clip(np.round((1.0 - uv[:, 1] % 1.0) * (height - 1)).astype(np.int64), 0, height - 1)
        srgb = pixels[y, x].astype(np.float64) / 255.0
        factor = getattr(getattr(geometry.visual, "material", None), "baseColorFactor", None)
        if factor is not None:
            srgb = srgb * (np.asarray(factor[:3], dtype=np.float64) / 255.0)
        colours = np.full((len(uv), 4), 255, dtype=np.uint8)
        colours[:, :3] = np.round(srgb * 255.0).astype(np.uint8)
        geometry.visual = trimesh.visual.ColorVisuals(geometry, vertex_colors=colours)
    return copy


_3MF_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_3MF_MATERIAL = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"


def _write_coloured_3mf(mesh: trimesh.Trimesh, dest: Path) -> None:
    """3MF core + Materials extension: one colour per vertex (m:colorgroup). The
    mesh's vertex colours are already sRGB (:func:`_srgb_colours_with_textures`)."""
    per_vertex = _vertex_colours(mesh)
    assert per_vertex is not None
    srgb = per_vertex[:, :3]
    colours = "".join(f'<m:color color="#{r:02X}{g:02X}{b:02X}"/>' for r, g, b in srgb)
    vertices = "".join(
        f'<vertex x="{x:.6f}" y="{y:.6f}" z="{z:.6f}"/>' for x, y, z in mesh.vertices
    )
    triangles = "".join(
        f'<triangle v1="{a}" v2="{b}" v3="{c}" p1="{a}" p2="{b}" p3="{c}"/>'
        for a, b, c in mesh.faces
    )
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{_3MF_CORE}" '
        f'xmlns:m="{_3MF_MATERIAL}" requiredextensions="">'
        '<metadata name="Application">AI 3D Studio</metadata>'
        f'<resources><m:colorgroup id="1">{colours}</m:colorgroup>'
        f'<object id="2" type="model" pid="1" pindex="0"><mesh>'
        f"<vertices>{vertices}</vertices><triangles>{triangles}</triangles>"
        "</mesh></object></resources>"
        '<build><item objectid="2"/></build></model>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" '
        'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
    )
    dest.write_bytes(
        _zip(
            {
                "[Content_Types].xml": content_types.encode(),
                "_rels/.rels": relationships.encode(),
                "3D/3dmodel.model": model.encode(),
            }
        )
    )


def _threemf_colours(path: Path) -> bool:
    """True when the 3MF's triangles use a colour group with more than one colour."""
    with zipfile.ZipFile(path) as archive:
        name = next((n for n in archive.namelist() if n.endswith(".model")), None)
        if name is None:
            return False
        root = ElementTree.fromstring(archive.read(name))  # noqa: S314 - our own file
    ns = {"c": _3MF_CORE, "m": _3MF_MATERIAL}
    distinct = {e.get("color") for e in root.iterfind(".//m:colorgroup/m:color", ns)}
    coloured_triangles = root.find(".//c:triangle[@p1]", ns)
    return len(distinct) > 1 and coloured_triangles is not None


def _write_usdz(scene: trimesh.Scene, dest: Path) -> None:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, UsdUtils, Vt  # noqa: PLC0415 - optional

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        stage_path = root / "model.usdc"
        stage = Usd.Stage.CreateNew(str(stage_path))
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
        UsdGeom.SetStageMetersPerUnit(stage, 1.0)
        world = UsdGeom.Xform.Define(stage, "/Model")
        stage.SetDefaultPrim(world.GetPrim())
        textures: list[str] = []
        for index, mesh in enumerate(m for m in scene.dump() if isinstance(m, trimesh.Trimesh)):
            path = f"/Model/Mesh_{index}"
            usd_mesh = UsdGeom.Mesh.Define(stage, path)
            usd_mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(mesh.vertices.astype(np.float32)))
            usd_mesh.CreateFaceVertexCountsAttr(Vt.IntArray([3] * len(mesh.faces)))
            usd_mesh.CreateFaceVertexIndicesAttr(
                Vt.IntArray.FromNumpy(mesh.faces.astype(np.int32).ravel())
            )
            usd_mesh.CreateNormalsAttr(
                Vt.Vec3fArray.FromNumpy(mesh.vertex_normals.astype(np.float32))
            )
            usd_mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
            usd_mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
            material = UsdShade.Material.Define(stage, f"{path}_Material")
            shader = UsdShade.Shader.Define(stage, f"{path}_Material/Surface")
            shader.CreateIdAttr("UsdPreviewSurface")
            texture = _usd_base_colour(
                stage, usd_mesh, mesh, material_path=f"{path}_Material", root=root, index=index
            )
            if texture is not None:
                if texture[0]:
                    textures.append(texture[0])
                shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
                    texture[1], texture[2]
                )
            else:
                colour = getattr(getattr(mesh.visual, "material", None), "baseColorFactor", None)
                rgb = (np.asarray(colour[:3]) / 255.0) if colour is not None else (0.8, 0.8, 0.8)
                shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(
                    Gf.Vec3f(*(float(c) for c in rgb))
                )
            shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.6)
            material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
            UsdShade.MaterialBindingAPI.Apply(usd_mesh.GetPrim()).Bind(material)
        stage.GetRootLayer().Save()
        if not UsdUtils.CreateNewARKitUsdzPackage(Sdf.AssetPath(str(stage_path)), str(dest)):
            raise ExportError("Pixar USD could not package the USDZ file.")


def _usd_base_colour(
    stage: Any, usd_mesh: Any, mesh: trimesh.Trimesh, *, material_path: str, root: Path, index: int
) -> tuple[str, Any, str] | None:
    """Wires the base colour to a texture or to per-vertex colours.

    Returns (texture file name or "", connectable source, output name), or None
    when the mesh has neither (a plain colour is used then).
    """
    from pxr import Sdf, UsdGeom, UsdShade, Vt  # noqa: PLC0415 - optional

    image = _base_image(mesh)
    uv = getattr(mesh.visual, "uv", None)
    if image is not None and uv is not None and len(uv):
        primvars = UsdGeom.PrimvarsAPI(usd_mesh)
        st = primvars.CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
        st.Set(Vt.Vec2fArray.FromNumpy(np.asarray(uv, dtype=np.float32)))
        name = f"texture_{index}.png"
        image.convert("RGB").save(root / name)
        reader = UsdShade.Shader.Define(stage, f"{material_path}/StReader")
        reader.CreateIdAttr("UsdPrimvarReader_float2")
        reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
        texture = UsdShade.Shader.Define(stage, f"{material_path}/BaseColor")
        texture.CreateIdAttr("UsdUVTexture")
        texture.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(name)
        texture.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
            reader.ConnectableAPI(), "result"
        )
        texture.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
        return name, texture.ConnectableAPI(), "rgb"
    colours = _vertex_colours(mesh)
    if colours is None or not _has_colours(mesh):
        return None
    # USD colours are linear, like glTF's: written as they are.
    display = UsdGeom.PrimvarsAPI(usd_mesh).CreatePrimvar(
        "displayColor", Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.vertex
    )
    display.Set(Vt.Vec3fArray.FromNumpy(colours[:, :3].astype(np.float32) / 255.0))
    reader = UsdShade.Shader.Define(stage, f"{material_path}/ColourReader")
    reader.CreateIdAttr("UsdPrimvarReader_float3")
    reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("displayColor")
    reader.CreateOutput("result", Sdf.ValueTypeNames.Float3)
    return "", reader.ConnectableAPI(), "result"


def _blender_error(action: str, exc: Exception) -> ExportError:
    if isinstance(exc, BlenderFailedError):
        return ExportError(f"Blender could not {action}: {exc.message}")
    return ExportError(f"Blender could not {action}: Blender was not found.")


def _quads_note(count: int) -> str:
    return (
        f"{count:,} quads from retopology are kept as quads (the GLB stores each as two triangles)."
    )


def _write_fbx(
    scene: trimesh.Scene, dest: Path, quad_pairs: dict[str, np.ndarray] | None = None
) -> list[str]:
    """GLB → Blender (glTF importer) → FBX with embedded textures. Returns extra notes."""
    with tempfile.TemporaryDirectory(prefix="fbx-", dir=dest.parent) as tmp:
        source = Path(tmp) / "source.glb"
        source.write_bytes(scene.export(file_type="glb"))  # type: ignore[no-untyped-call]
        centres = quads.pair_centres(scene, quad_pairs) if quad_pairs else None
        return write_fbx_from_glb(source, dest, quad_centres=centres)


def write_fbx_from_glb(
    source: Path, dest: Path, *, quad_centres: np.ndarray | None = None
) -> list[str]:
    """FBX straight from a GLB file (Blender imports it): the caller need not hold the
    model in memory while Blender works. ``quad_centres`` (P, 2, 3): the two triangle
    centres of each quad from retopology, which Blender joins (D60)."""
    with tempfile.TemporaryDirectory(prefix="fbx-", dir=dest.parent) as tmp:
        request = {"input": str(source.resolve()), "output": str(dest.resolve())}
        if quad_centres is not None and len(quad_centres):
            centres = Path(tmp) / "quad-centres.npy"
            np.save(centres, quad_centres)
            request["quad_centres"] = str(centres)
        try:
            result = run_blender(
                "export_fbx",
                request,
                workdir=Path(tmp),
                timeout=BLENDER_TIMEOUT,
            )
        except (BlenderFailedError, BlenderNotFoundError) as exc:
            raise _blender_error("write the FBX file", exc) from exc
    notes = [_quads_note(result["quads_joined"])] if result.get("quads_joined") else []
    for dropped in result.get("dropped_colour_factors", []):
        rgb = ", ".join(f"{c:g}" for c in dropped["factor"])
        notes.append(
            f"Material “{dropped['material']}” multiplies its texture by a colour factor "
            f"({rgb}); FBX has no such factor, so the texture is written without it."
        )
    return notes


def _write_obj(
    scene: trimesh.Scene, dest: Path, quad_pairs: dict[str, np.ndarray] | None
) -> list[str]:
    """OBJ, MTL and textures, zipped; quads from retopology joined back (D60)."""
    result = export_obj(  # type: ignore[no-untyped-call]
        _srgb_colour_scene(scene),
        include_texture=True,
        return_texture=True,
        mtl_name="model.mtl",
    )
    text, extra = result if isinstance(result, tuple) else (result, {})
    joined = 0
    if quad_pairs:
        text, joined = quads.obj_with_quads(text, scene, quad_pairs)
    # Written into the zip a piece at a time: the text is not also held encoded and
    # compressed in memory (each is about as large as the text on big models).
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted([*extra, "model.obj"]):
            if name != "model.obj":
                archive.writestr(name, extra[name])
                continue
            with archive.open(name, "w", force_zip64=True) as entry:
                for start in range(0, len(text), _TEXT_PIECE):
                    entry.write(text[start : start + _TEXT_PIECE].encode())
    return [_quads_note(joined)] if joined else []


def write(
    scene: trimesh.Scene,
    fmt: ExportFormat,
    dest: Path,
    *,
    quad_pairs: dict[str, np.ndarray] | None = None,
) -> list[str]:
    """Write ``scene`` as ``fmt`` to ``dest``; returns notes specific to this file.

    ``quad_pairs`` (from retopology, D60): OBJ and FBX files join those triangle
    pairs into quads; other formats hold triangles only."""
    info = format_info(fmt)
    if not info.available:
        raise ExportError(info.reason or f"{info.label} export is not available.")
    if fmt == ExportFormat.GLB:
        dest.write_bytes(scene.export(file_type="glb"))  # type: ignore[no-untyped-call]
    elif fmt == ExportFormat.GLTF:
        files = scene.export(file_type="gltf")  # type: ignore[no-untyped-call]
        dest.write_bytes(_zip(files))
    elif fmt == ExportFormat.OBJ:
        return _write_obj(scene, dest, quad_pairs)
    elif fmt == ExportFormat.STL:
        dest.write_bytes(_as_bytes(_for_printing(scene).export(file_type="stl")))
    elif fmt == ExportFormat.THREE_MF:
        mesh = _for_printing(_srgb_colours_with_textures(scene))
        if _has_colours(mesh):
            _write_coloured_3mf(mesh, dest)
        else:
            dest.write_bytes(_as_bytes(mesh.export(file_type="3mf")))
    elif fmt == ExportFormat.USDZ:
        _write_usdz(scene, dest)
    elif fmt == ExportFormat.FBX:
        return _write_fbx(scene, dest, quad_pairs)
    else:  # pragma: no cover - every format is handled above
        raise ExportError(f"{info.label} export is not implemented.")
    return []


# -- re-opening ------------------------------------------------------------------------


def _as_scene(loaded: object) -> trimesh.Scene:
    if not isinstance(loaded, trimesh.Scene):
        raise ExportError("The exported file did not open as a 3D scene.")
    return loaded


def _read_zip_scene(path: Path, entry: str) -> trimesh.Scene:
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            target = (Path(tmp) / name).resolve()
            if not str(target).startswith(str(Path(tmp).resolve())):
                raise ExportError("Unsafe path in archive.")
        archive.extractall(tmp)
        return normalise_vertex_colours(
            _as_scene(trimesh.load(Path(tmp) / entry, force="scene", process=False))
        )


def _usdz_facts(path: Path) -> Facts:
    from pxr import Usd, UsdGeom  # noqa: PLC0415 - optional

    stage = Usd.Stage.Open(str(path))
    if stage is None:
        raise ExportError("The USDZ file could not be opened.")
    triangles = 0
    textured = False
    coloured = False
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    box = cache.ComputeWorldBound(stage.GetDefaultPrim()).ComputeAlignedRange()
    for prim in stage.Traverse():
        if prim.IsA(UsdGeom.Mesh):
            counts = UsdGeom.Mesh(prim).GetFaceVertexCountsAttr().Get() or []
            triangles += sum(c - 2 for c in counts)
            display = UsdGeom.PrimvarsAPI(prim).GetPrimvar("displayColor")
            values = display.Get() if display and display.HasValue() else None
            points = UsdGeom.Mesh(prim).GetPointsAttr().Get() or []
            if values is not None and len(values) == len(points) and len(values) > 1:
                coloured = coloured or bool(np.asarray(values).std() > 0.002)
        if prim.GetAttribute("inputs:file").IsValid():
            textured = True
    size = [float(v) for v in box.GetSize()] if not box.IsEmpty() else [0.0, 0.0, 0.0]
    return Facts(triangles=triangles, size=size, textured=textured, coloured=coloured)


def _fbx_facts(path: Path) -> Facts:
    """Import the FBX in a fresh Blender run (FBX importer) and report what it holds.

    A texture only counts when its image has pixels, feeds the material, and
    came from inside the file (embedded), not from an image lying next to it.
    """
    with path.open("rb") as handle:
        if handle.read(len(_FBX_MAGIC)) != _FBX_MAGIC:
            raise ExportError("The exported file is not a binary FBX file.")
    with tempfile.TemporaryDirectory(prefix="fbx-check-") as tmp:
        try:
            found = run_blender(
                "inspect",
                {"input": str(path.resolve())},
                workdir=Path(tmp),
                timeout=BLENDER_TIMEOUT,
            )
        except (BlenderFailedError, BlenderNotFoundError) as exc:
            raise _blender_error("re-open the FBX file", exc) from exc
    images = found.get("images", [])
    return Facts(
        triangles=int(found["triangles"]),
        size=[float(v) for v in found["size"]],
        textured=any(i["has_pixels"] and i["linked"] and i["packed"] for i in images),
        extra={
            "reader": f"Blender {found.get('blender', '')} FBX importer".replace("  ", " "),
            "textured_meshes": int(found.get("textured_objects", 0)),
            "quads": int(found.get("quads", 0)),
        },
        coloured=int(found.get("coloured_objects", 0)) > 0,
    )


def read_back(path: Path, fmt: ExportFormat) -> Facts:
    """Open the exported file with an independent reader."""
    try:
        if fmt == ExportFormat.USDZ:
            return _usdz_facts(path)
        if fmt == ExportFormat.FBX:
            return _fbx_facts(path)
        if fmt == ExportFormat.GLTF:
            scene = _read_zip_scene(path, "model.gltf")
        elif fmt == ExportFormat.OBJ:
            # Quads counted line by line, before the model is re-opened.
            with (
                zipfile.ZipFile(path) as archive,
                archive.open("model.obj") as entry,
                io.TextIOWrapper(entry, encoding="utf-8", errors="replace") as lines,
            ):
                quad_count, _ = quads.count_obj_faces(lines)
            scene = _read_zip_scene(path, "model.obj")
        else:
            file_type = {"glb": "glb", "stl": "stl", "3mf": "3mf"}[fmt.value]
            scene = _as_scene(trimesh.load(path, file_type=file_type, force="scene", process=False))
        facts = scene_facts(normalise_vertex_colours(scene))
        if fmt == ExportFormat.OBJ:
            facts.extra["quads"] = quad_count
        if fmt == ExportFormat.THREE_MF:
            # trimesh reads 3MF geometry but not colour groups; those are checked in the XML.
            facts.coloured = _threemf_colours(path)
    except ExportError:
        raise
    except Exception as exc:  # readers raise many exception types for bad files
        raise ExportError(f"The exported file could not be re-opened: {exc}") from exc
    return facts


def validate(
    path: Path, fmt: ExportFormat, source: Facts, *, quads_expected: int = 0
) -> dict[str, Any]:
    """Re-open ``path`` and compare it with the source. Raises ExportError on mismatch.

    ``quads_expected``: quads from retopology the file should hold; the result says
    how many the re-opened file actually has (never more than were made)."""
    info = format_info(fmt)
    found = read_back(path, fmt)
    printing = fmt in {ExportFormat.STL, ExportFormat.THREE_MF}
    unit = 1000.0 if printing else 1.0
    x, y, z = source.size
    expected = [v * unit for v in ((x, z, y) if printing else (x, y, z))]  # Z up for printing
    problems: list[str] = []
    if found.triangles != source.triangles:
        problems.append(f"{found.triangles:,} triangles instead of {source.triangles:,}")
    for axis, (got, want) in zip("xyz", zip(found.size, expected, strict=True), strict=True):
        if abs(got - want) > SIZE_TOLERANCE * max(abs(want), 1e-9):
            problems.append(f"{axis} size {got:.6g} instead of {want:.6g}")
    if info.keeps_texture and source.textured and not found.textured:
        problems.append("the texture is missing")
    elif fmt == ExportFormat.FBX and found.extra.get("textured_meshes", 0) < source.extra.get(
        "textured_meshes", 0
    ):
        problems.append(
            f"only {found.extra['textured_meshes']} of {source.extra['textured_meshes']} "
            "textured meshes have their texture"
        )
    if info.keeps_colour and source.coloured and not found.coloured:
        problems.append("the colours are missing")
    found_quads = int(found.extra.get("quads", 0))
    if problems:
        raise ExportError("The exported file does not match the model: " + "; ".join(problems))
    reader = found.extra.get("reader") or ("Pixar USD" if fmt == ExportFormat.USDZ else "trimesh")
    return {
        "reopened_with": reader,
        "triangles": found.triangles,
        "size": [round(v, 6) for v in found.size],
        "units": "millimetres" if unit == 1000.0 else "model units (metres in glTF)",
        "textured": found.textured,
        "coloured": found.coloured,
        "axes": "Z up" if printing else "Y up",
        "file_bytes": path.stat().st_size,
        # Four-sided faces in the re-opened file (OBJ and FBX can keep quads).
        "quads": found_quads,
        "quads_expected": quads_expected,
    }
