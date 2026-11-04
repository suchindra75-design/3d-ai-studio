"""Exporting a version to other formats — and proving each file by re-opening it.

Every export is read back with an independent reader (trimesh; Pixar USD for
USDZ; for FBX, Blender's FBX importer in a separate headless run) and compared
with the source: same triangle count, same size (after the unit conversion the
format implies), texture present when the format carries one. A file that fails
is never offered for download.

Units: glTF is in metres. STL and 3MF are read as millimetres by slicers, so
they are written in millimetres. When the model's real scale is unknown the
numbers are still the model's own units — the export says so. FBX files
declare the metre as their unit and are Y-up, like glTF.
"""

import importlib.util
import io
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from PIL import Image
from trimesh.exchange.obj import export_obj

from ai3d.models.enums import ExportFormat
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
            ),
        ),
        FormatInfo(
            ExportFormat.STL,
            "STL",
            "stl",
            "model/stl",
            True,
            None,
            False,
            ("Geometry only: no colour or texture. Written in millimetres.",),
        ),
        FormatInfo(
            ExportFormat.THREE_MF,
            "3MF",
            "3mf",
            "model/3mf",
            True,
            None,
            False,
            ("For 3D printing; written in millimetres. Textures are not included.",),
        ),
        FormatInfo(
            ExportFormat.USDZ,
            "USDZ",
            "usdz",
            "model/vnd.usdz+zip",
            usd,
            None if usd else "Needs Pixar USD (the usdz extra: usd-core).",
            True,
            ("For Apple AR Quick Look; base-colour texture only.",),
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
                "metre. Only the base colour is carried as a texture.",
            ),
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
    )


def _base_image(mesh: trimesh.Trimesh) -> Image.Image | None:
    material = getattr(mesh.visual, "material", None)
    for attr in ("baseColorTexture", "image"):
        image = getattr(material, attr, None) if material is not None else None
        if isinstance(image, Image.Image):
            return image
    return None


# -- writers ---------------------------------------------------------------------------


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


def _in_millimetres(scene: trimesh.Scene) -> trimesh.Trimesh:
    mesh: trimesh.Trimesh = scene.to_mesh()  # all instances in world coordinates, one mesh
    mesh.apply_scale(1000.0)  # type: ignore[no-untyped-call]
    return mesh


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
            image = _base_image(mesh)
            uv = getattr(mesh.visual, "uv", None)
            if image is not None and uv is not None and len(uv):
                primvars = UsdGeom.PrimvarsAPI(usd_mesh)
                st = primvars.CreatePrimvar(
                    "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex
                )
                st.Set(Vt.Vec2fArray.FromNumpy(np.asarray(uv, dtype=np.float32)))
                name = f"texture_{index}.png"
                image.convert("RGB").save(root / name)
                textures.append(name)
                reader = UsdShade.Shader.Define(stage, f"{path}_Material/StReader")
                reader.CreateIdAttr("UsdPrimvarReader_float2")
                reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
                texture = UsdShade.Shader.Define(stage, f"{path}_Material/BaseColor")
                texture.CreateIdAttr("UsdUVTexture")
                texture.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(name)
                texture.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
                    reader.ConnectableAPI(), "result"
                )
                texture.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
                shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
                    texture.ConnectableAPI(), "rgb"
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


def _blender_error(action: str, exc: Exception) -> ExportError:
    if isinstance(exc, BlenderFailedError):
        return ExportError(f"Blender could not {action}: {exc.message}")
    return ExportError(f"Blender could not {action}: Blender was not found.")


def _write_fbx(scene: trimesh.Scene, dest: Path) -> list[str]:
    """GLB → Blender (glTF importer) → FBX with embedded textures. Returns extra notes."""
    with tempfile.TemporaryDirectory(prefix="fbx-", dir=dest.parent) as tmp:
        source = Path(tmp) / "source.glb"
        source.write_bytes(scene.export(file_type="glb"))  # type: ignore[no-untyped-call]
        try:
            result = run_blender(
                "export_fbx",
                {"input": str(source), "output": str(dest.resolve())},
                workdir=Path(tmp),
                timeout=BLENDER_TIMEOUT,
            )
        except (BlenderFailedError, BlenderNotFoundError) as exc:
            raise _blender_error("write the FBX file", exc) from exc
    notes = []
    for dropped in result.get("dropped_colour_factors", []):
        rgb = ", ".join(f"{c:g}" for c in dropped["factor"])
        notes.append(
            f"Material “{dropped['material']}” multiplies its texture by a colour factor "
            f"({rgb}); FBX has no such factor, so the texture is written without it."
        )
    return notes


def write(scene: trimesh.Scene, fmt: ExportFormat, dest: Path) -> list[str]:
    """Write ``scene`` as ``fmt`` to ``dest``; returns notes specific to this file."""
    info = format_info(fmt)
    if not info.available:
        raise ExportError(info.reason or f"{info.label} export is not available.")
    if fmt == ExportFormat.GLB:
        dest.write_bytes(scene.export(file_type="glb"))  # type: ignore[no-untyped-call]
    elif fmt == ExportFormat.GLTF:
        files = scene.export(file_type="gltf")  # type: ignore[no-untyped-call]
        dest.write_bytes(_zip(files))
    elif fmt == ExportFormat.OBJ:
        result = export_obj(  # type: ignore[no-untyped-call]
            scene, include_texture=True, return_texture=True, mtl_name="model.mtl"
        )
        text, extra = result if isinstance(result, tuple) else (result, {})
        dest.write_bytes(_zip({"model.obj": text.encode(), **extra}))
    elif fmt == ExportFormat.STL:
        dest.write_bytes(_as_bytes(_in_millimetres(scene).export(file_type="stl")))
    elif fmt == ExportFormat.THREE_MF:
        dest.write_bytes(_as_bytes(_in_millimetres(scene).export(file_type="3mf")))
    elif fmt == ExportFormat.USDZ:
        _write_usdz(scene, dest)
    elif fmt == ExportFormat.FBX:
        return _write_fbx(scene, dest)
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
        return _as_scene(trimesh.load(Path(tmp) / entry, force="scene", process=False))


def _usdz_facts(path: Path) -> Facts:
    from pxr import Usd, UsdGeom  # noqa: PLC0415 - optional

    stage = Usd.Stage.Open(str(path))
    if stage is None:
        raise ExportError("The USDZ file could not be opened.")
    triangles = 0
    textured = False
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    box = cache.ComputeWorldBound(stage.GetDefaultPrim()).ComputeAlignedRange()
    for prim in stage.Traverse():
        if prim.IsA(UsdGeom.Mesh):
            counts = UsdGeom.Mesh(prim).GetFaceVertexCountsAttr().Get() or []
            triangles += sum(c - 2 for c in counts)
        if prim.GetAttribute("inputs:file").IsValid():
            textured = True
    size = [float(v) for v in box.GetSize()] if not box.IsEmpty() else [0.0, 0.0, 0.0]
    return Facts(triangles=triangles, size=size, textured=textured)


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
        },
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
            scene = _read_zip_scene(path, "model.obj")
        else:
            file_type = {"glb": "glb", "stl": "stl", "3mf": "3mf"}[fmt.value]
            scene = _as_scene(trimesh.load(path, file_type=file_type, force="scene", process=False))
    except ExportError:
        raise
    except Exception as exc:  # readers raise many exception types for bad files
        raise ExportError(f"The exported file could not be re-opened: {exc}") from exc
    return scene_facts(scene)


def validate(path: Path, fmt: ExportFormat, source: Facts) -> dict[str, Any]:
    """Re-open ``path`` and compare it with the source. Raises ExportError on mismatch."""
    info = format_info(fmt)
    found = read_back(path, fmt)
    unit = 1000.0 if fmt in {ExportFormat.STL, ExportFormat.THREE_MF} else 1.0
    expected = [v * unit for v in source.size]
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
    if problems:
        raise ExportError("The exported file does not match the model: " + "; ".join(problems))
    reader = found.extra.get("reader") or ("Pixar USD" if fmt == ExportFormat.USDZ else "trimesh")
    return {
        "reopened_with": reader,
        "triangles": found.triangles,
        "size": [round(v, 6) for v in found.size],
        "units": "millimetres" if unit == 1000.0 else "model units (metres in glTF)",
        "textured": found.textured,
        "file_bytes": path.stat().st_size,
    }
