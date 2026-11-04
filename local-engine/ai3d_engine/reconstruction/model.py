"""Reading a COLMAP text model (``model_converter --output_type TXT``).

Only what the pipeline needs: registered images with their poses, and the
sparse points with their reprojection error and track length.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class RegisteredImage:
    name: str
    # World-to-camera rotation (rows are the camera's x, y, z axes in world coordinates).
    rotation: FloatArray
    center: FloatArray


@dataclass(frozen=True, slots=True)
class SparseModel:
    images: list[RegisteredImage]
    points: FloatArray  # (n, 3)
    colors: npt.NDArray[np.uint8]  # (n, 3)
    errors: FloatArray  # reprojection error per point, pixels
    track_lengths: npt.NDArray[np.int64]

    @property
    def mean_reprojection_error(self) -> float:
        return float(self.errors.mean()) if len(self.errors) else 0.0

    @property
    def mean_track_length(self) -> float:
        return float(self.track_lengths.mean()) if len(self.track_lengths) else 0.0


def _quaternion_to_matrix(qw: float, qx: float, qy: float, qz: float) -> FloatArray:
    q = np.array([qw, qx, qy, qz], dtype=np.float64)
    w, x, y, z = q / np.linalg.norm(q)
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def _data_lines(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line and not line.startswith("#")]


def read_text_model(directory: Path) -> SparseModel:
    # Two lines per image: pose + name, then 2D observations (empty if none).
    lines = [
        line
        for line in (directory / "images.txt").read_text().splitlines()
        if not line.startswith("#")
    ]
    images: list[RegisteredImage] = []
    for header in lines[0::2]:
        parts = header.split(maxsplit=9)
        if len(parts) != 10:
            continue
        qw, qx, qy, qz, tx, ty, tz = (float(v) for v in parts[1:8])
        rotation = _quaternion_to_matrix(qw, qx, qy, qz)
        center = -rotation.T @ np.array([tx, ty, tz])
        images.append(RegisteredImage(name=parts[9], rotation=rotation, center=center))

    rows = [line.split() for line in _data_lines(directory / "points3D.txt")]
    points = np.array([[float(v) for v in r[1:4]] for r in rows], dtype=np.float64).reshape(-1, 3)
    colors = np.array([[int(v) for v in r[4:7]] for r in rows], dtype=np.uint8).reshape(-1, 3)
    errors = np.array([float(r[7]) for r in rows], dtype=np.float64)
    tracks = np.array([(len(r) - 8) // 2 for r in rows], dtype=np.int64)
    return SparseModel(images, points, colors, errors, tracks)


# The x axes of upright photos lie in one (horizontal) plane: their smallest singular
# value is ~0. Above this ratio of the two smallest, photos were rolled (e.g. portrait
# shots, or shots from above at any angle) and the x axes say little about gravity.
ROLL_CONDITIONING = 0.35


def estimate_up(images: list[RegisteredImage], points: FloatArray | None = None) -> FloatArray:
    """World "up" for a capture of an object.

    Upright photos (not rolled): the camera's x axis is horizontal whatever the tilt,
    so gravity is the direction most perpendicular to all x axes (exact when it holds).
    Rolled photos (found on a real capture with many portrait and top-down shots, where
    the x-axis rule was 92° off): the cameras surround the object from above, so the
    average direction from the object to the cameras is up (5.6° from the table there).
    """
    if len(images) < 2:
        return np.array([0.0, 1.0, 0.0])
    x_axes = np.array([image.rotation[0] for image in images])
    _, singular, vt = np.linalg.svd(x_axes, full_matrices=True)
    camera_up = -np.array([image.rotation[1] for image in images]).mean(axis=0)
    upright = len(singular) >= 3 and singular[2] < ROLL_CONDITIONING * singular[1]
    if not upright and points is not None and len(points):
        centres = np.array([image.center for image in images])
        towards_cameras = (centres - np.median(points, axis=0)).mean(axis=0)
        norm = float(np.linalg.norm(towards_cameras))
        if norm > 1e-9:
            return np.asarray(towards_cameras / norm, dtype=np.float64)
    up = vt[-1]
    if float(up @ camera_up) < 0:
        up = -up
    return np.asarray(up / np.linalg.norm(up), dtype=np.float64)


def upright_transform(up: FloatArray, points: FloatArray) -> FloatArray:
    """4×4 matrix: rotate ``up`` onto +Y, centre the points horizontally, floor at y = 0.

    The floor is the 2nd percentile of heights, so a few stray points below the
    object do not lift it. Scale is not changed: it is unknown.
    """
    target = np.array([0.0, 1.0, 0.0])
    axis = np.cross(up, target)
    sin, cos = float(np.linalg.norm(axis)), float(up @ target)
    if sin < 1e-9:
        rotation = np.eye(3) if cos > 0 else np.diag([1.0, -1.0, -1.0])
    else:
        k = axis / sin
        skew = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
        rotation = np.eye(3) + sin * skew + (1 - cos) * skew @ skew
    rotated = points @ rotation.T if len(points) else points
    shift = np.zeros(3)
    if len(rotated):
        shift[0] = -float(np.median(rotated[:, 0]))
        shift[2] = -float(np.median(rotated[:, 2]))
        shift[1] = -float(np.percentile(rotated[:, 1], 2))
    matrix = np.eye(4)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = shift
    return matrix
