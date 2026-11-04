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
    """Convert COLMAP's PLY (textured or not) to GLB in the upright frame."""
    loaded = trimesh.load(mesh_path, process=False)
    if not isinstance(loaded, trimesh.Trimesh):
        raise ValueError("The reconstructed surface is not a triangle mesh.")
    loaded.apply_transform(np.asarray(transform, dtype=np.float64))
    # COLMAP's Delaunay output can include unreferenced vertices and zero-area faces.
    loaded.update_faces(loaded.nondegenerate_faces())
    loaded.remove_unreferenced_vertices()
    textured = loaded.visual is not None and loaded.visual.kind == "texture"
    scene = trimesh.Scene()
    scene.add_geometry(loaded, node_name="Reconstruction", geom_name="Reconstruction")
    data = scene.export(file_type="glb")  # type: ignore[no-untyped-call]
    assert isinstance(data, bytes)
    out_path.write_bytes(data)
    return {"textured": textured, "faces": len(loaded.faces), "vertices": len(loaded.vertices)}
