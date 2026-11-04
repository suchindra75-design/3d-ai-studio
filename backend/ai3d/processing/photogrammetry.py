"""Photo preparation and mesh conversion around the COLMAP pipeline.

Photos stay on this machine: they are written to the job's private workspace
(upright, without GPS), reconstructed there, and the workspace is deleted.
"""

from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from PIL import Image, ImageOps

_ORIENTATION = 0x0112
_GPS_IFD = 0x8825


def prepare_photo(source: Path, dest_stem: Path) -> Path:
    """Write an upright copy for COLMAP.

    EXIF orientation is applied (so "up" in every photo is up in the world,
    which the upright alignment relies on); camera EXIF such as focal length is
    kept because COLMAP uses it, GPS is dropped. JPEG stays JPEG, others → PNG.
    """
    with Image.open(source) as img:
        exif = img.getexif()
        upright = ImageOps.exif_transpose(img)
        exif.pop(_ORIENTATION, None)
        exif.pop(_GPS_IFD, None)
        if img.format == "JPEG":
            path = dest_stem.with_suffix(".jpg")
            upright.convert("RGB").save(path, "JPEG", quality=95, exif=exif.tobytes())
        else:
            path = dest_stem.with_suffix(".png")
            upright.convert("RGB").save(path, "PNG")
    return path


def mesh_to_glb(mesh_path: Path, transform: list[list[float]], out_path: Path) -> dict[str, Any]:
    """Convert a reconstructed surface to GLB in the upright frame.

    COLMAP writes a PLY (textured or not); OpenMVS writes an OBJ whose texture can be
    split into several atlases, which loads as one mesh per atlas.
    """
    loaded = trimesh.load(mesh_path, process=False)
    if isinstance(loaded, trimesh.Trimesh):
        parts = [loaded]
    elif isinstance(loaded, trimesh.Scene):
        parts = [g for g in loaded.dump() if isinstance(g, trimesh.Trimesh)]
    else:
        parts = []
    if not parts:
        raise ValueError("The reconstructed surface is not a triangle mesh.")
    matrix = np.asarray(transform, dtype=np.float64)
    scene = trimesh.Scene()
    for index, part in enumerate(parts):
        part.apply_transform(matrix)
        # Delaunay output can include unreferenced vertices and zero-area faces.
        part.update_faces(part.nondegenerate_faces())
        part.remove_unreferenced_vertices()
        name = "Reconstruction" if len(parts) == 1 else f"Reconstruction_{index}"
        scene.add_geometry(part, node_name=name, geom_name=name)
    textured = any(p.visual is not None and p.visual.kind == "texture" for p in parts)
    data = scene.export(file_type="glb")  # type: ignore[no-untyped-call]
    assert isinstance(data, bytes)
    out_path.write_bytes(data)
    return {
        "textured": textured,
        "faces": sum(len(p.faces) for p in parts),
        "vertices": sum(len(p.vertices) for p in parts),
    }
