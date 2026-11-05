"""Turning an existing 3D file into a GLB the app can store and check.

Accepted: GLB, OBJ, STL, PLY, 3MF and FBX (converted by Blender) files, and ZIP
archives holding one model with its companions (an OBJ with its MTL and textures,
a glTF with its .bin and images). Archives are unpacked with limits on entry count
and size, and no entry may leave the working folder.

Units and "up": glTF is metres and Y-up by definition. Other formats say
neither, so the caller tells: STL and 3MF are usually millimetres and Z-up
(slicers), OBJ and PLY metres and Y-up. Whether the file's size is the real
object's size is a separate question the caller answers.
"""

import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import numpy as np
import trimesh

from ai3d.core.errors import InvalidInputError
from ai3d.processing.mesh import load_model_scene, normalise_vertex_colours
from ai3d_engine.blender import BlenderFailedError, check_blender, run_blender

Units = Literal["m", "cm", "mm", "in"]
UpAxis = Literal["y", "z"]

METRES_PER_UNIT: dict[str, float] = {"m": 1.0, "cm": 0.01, "mm": 0.001, "in": 0.0254}
UNIT_NAMES = {"m": "metres", "cm": "centimetres", "mm": "millimetres", "in": "inches"}

MODEL_SUFFIXES = (".glb", ".gltf", ".obj", ".stl", ".ply", ".3mf", ".fbx")
# What each format usually means when the caller does not say.
DEFAULTS: dict[str, tuple[Units, UpAxis]] = {
    ".stl": ("mm", "z"),
    ".3mf": ("mm", "z"),
    ".obj": ("m", "y"),
    ".ply": ("m", "y"),
}

FBX_TIMEOUT = 600.0
MAX_ARCHIVE_ENTRIES = 500
MAX_ARCHIVE_BYTES = 1024**3  # unpacked


@dataclass(frozen=True, slots=True)
class Converted:
    glb: Path
    format: str  # e.g. "STL", "OBJ (in a ZIP)"
    units: str  # "metres (glTF)" or the chosen unit
    up: str
    source_name: str


def _safe_extract(archive: Path, dest: Path) -> list[Path]:
    """Unpack a ZIP with limits; returns the files written."""
    try:
        bundle = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise InvalidInputError("The ZIP archive could not be opened.") from exc
    with bundle:
        entries = [e for e in bundle.infolist() if not e.is_dir()]
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise InvalidInputError(f"The archive has more than {MAX_ARCHIVE_ENTRIES} files.")
        if sum(e.file_size for e in entries) > MAX_ARCHIVE_BYTES:
            raise InvalidInputError("The archive unpacks to more than 1 GB.")
        written: list[Path] = []
        root = dest.resolve()
        for entry in entries:
            name = PurePosixPath(entry.filename.replace("\\", "/"))
            mode = (entry.external_attr >> 16) & 0o170000
            if name.is_absolute() or ".." in name.parts or mode == 0o120000:  # 0o120000: symlink
                raise InvalidInputError(f"The archive entry {entry.filename!r} is not allowed.")
            target = (root / Path(*name.parts)).resolve()
            if root not in target.parents:
                raise InvalidInputError(f"The archive entry {entry.filename!r} is not allowed.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(entry) as src, target.open("wb") as out:
                remaining = entry.file_size
                while chunk := src.read(1024 * 1024):
                    remaining -= len(chunk)
                    if remaining < 0:  # the archive lied about the size
                        raise InvalidInputError("The archive is damaged.")
                    out.write(chunk)
            written.append(target)
    return written


def _main_model(files: list[Path]) -> Path:
    models = [f for f in files if f.suffix.lower() in MODEL_SUFFIXES and "__MACOSX" not in f.parts]
    if not models:
        raise InvalidInputError(
            "The archive holds no 3D model (GLB, glTF, OBJ, STL, PLY, 3MF or FBX)."
        )
    if len(models) > 1:
        names = ", ".join(sorted(m.name for m in models)[:5])
        raise InvalidInputError(f"The archive holds more than one model ({names}); import one.")
    return models[0]


def _to_y_up() -> np.ndarray:
    """Z-up (x, y, z) → Y-up (x, z, -y): a -90° turn about X."""
    turn = np.eye(4)
    turn[:3, :3] = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]
    return turn


def _convert_fbx(path: Path, glb: Path, workdir: Path, label: str, name: str) -> Converted:
    """FBX goes through Blender, whose importer applies the file's units and axes."""
    problem = check_blender()
    if problem is not None:
        raise InvalidInputError(f"FBX files are converted with Blender. {problem}")
    try:
        run_blender(
            "import_fbx",
            {"input": str(path), "output": str(glb)},
            workdir=workdir,
            timeout=FBX_TIMEOUT,
        )
    except BlenderFailedError as exc:
        raise InvalidInputError(f"The FBX file could not be converted: {exc.message}") from exc
    return Converted(glb, label, "metres (from the FBX units)", "Y (from the FBX axes)", name)


def convert(
    source: Path,
    original_name: str,
    workdir: Path,
    *,
    units: Units | None = None,
    up: UpAxis | None = None,
) -> Converted:
    """Load ``source`` (named ``original_name`` by the user) and write it as GLB."""
    suffix = Path(original_name).suffix.lower()
    label = suffix.lstrip(".").upper()
    path = source
    if suffix == ".zip":
        unpacked = workdir / "unpacked"
        unpacked.mkdir()
        path = _main_model(_safe_extract(source, unpacked))
        suffix = path.suffix.lower()
        label = f"{suffix.lstrip('.').upper()} (in a ZIP)"
    elif suffix not in MODEL_SUFFIXES:
        raise InvalidInputError(
            "Import a GLB, OBJ, STL, PLY, 3MF or FBX file, or a ZIP with an OBJ or glTF and "
            "its files."
        )
    elif suffix == ".gltf":
        raise InvalidInputError(
            "A .gltf file needs its .bin and images: put them in a ZIP, or export a GLB."
        )

    glb = workdir / "imported.glb"
    if suffix == ".fbx":
        return _convert_fbx(path, glb, workdir, label, original_name)
    if suffix in {".glb", ".gltf"}:
        try:
            if suffix == ".glb":
                scene = load_model_scene(path)
            else:
                scene = normalise_vertex_colours(trimesh.load_scene(path, process=False))
        except Exception as exc:  # trimesh raises many types for unreadable input
            raise InvalidInputError("The file is not a readable glTF model.") from exc
        glb.write_bytes(scene.export(file_type="glb"))  # type: ignore[no-untyped-call]
        return Converted(glb, label, "metres (glTF)", "Y (glTF)", original_name)

    default_units, default_up = DEFAULTS[suffix]
    units = units or default_units
    up = up or default_up
    try:
        loaded = trimesh.load(path, file_type=suffix.lstrip("."), force="scene", process=False)
    except Exception as exc:
        raise InvalidInputError(f"The file is not a readable {label} model.") from exc
    if not isinstance(loaded, trimesh.Scene):
        raise InvalidInputError(f"The file is not a readable {label} model.")
    scene = normalise_vertex_colours(loaded)
    if not any(isinstance(g, trimesh.Trimesh) and len(g.faces) for g in scene.geometry.values()):
        raise InvalidInputError("The file contains no triangles.")
    transform = np.eye(4)
    transform[:3, :3] *= METRES_PER_UNIT[units]
    if up == "z":
        transform = _to_y_up() @ transform
    scene.apply_transform(transform)  # type: ignore[no-untyped-call]
    glb.write_bytes(scene.export(file_type="glb"))  # type: ignore[no-untyped-call]
    return Converted(glb, label, UNIT_NAMES[units], up.upper(), original_name)
