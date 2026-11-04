"""Multi-photo reconstruction with COLMAP.

features → matching → camera poses + sparse points (mapper) → point filtering
→ surface (Delaunay) → texture.

Without CUDA the surface is built from the *sparse* points: real, measured
geometry, but coarse. With CUDA, dense stereo (PatchMatch + fusion) gives a
detailed surface. Either way the scale is unknown (no reference object).
"""

import importlib.util
import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np

from ai3d_engine.reconstruction.colmap import (
    ColmapFailedError,
    ColmapRunner,
    StepProgress,
)
from ai3d_engine.reconstruction.model import (
    SparseModel,
    estimate_up,
    read_text_model,
    upright_transform,
)
from ai3d_engine.reconstruction.openmvs import OpenMVSFailedError, OpenMVSRunner

log = logging.getLogger(__name__)

MIN_PHOTOS = 3  # COLMAP's technical minimum; good results need far more
MIN_REGISTERED = 3
LOW_FEATURE_COUNT = 300
NO_START_CODE = "no_reconstruction"
NO_START_MESSAGE = (
    "COLMAP could not find two photos that overlap enough to start. Take photos closer "
    "together (about 60–80% overlap between neighbours) and make sure the object has visible "
    "texture."
)

Stage = Literal["features", "matching", "mapping", "filtering", "meshing", "texturing"]
# Share of the whole run each stage takes (for one progress bar).
STAGE_SPAN: dict[Stage, tuple[float, float]] = {
    "features": (0.0, 0.2),
    "matching": (0.2, 0.4),
    "mapping": (0.4, 0.75),
    "filtering": (0.75, 0.78),
    "meshing": (0.78, 0.88),
    "texturing": (0.88, 1.0),
}

ProgressFn = Callable[[Stage, float, str | None], None]
CancelFn = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class ReconstructionOptions:
    max_image_size: int = 2400
    max_num_features: int = 8192
    single_camera: bool = False
    matcher: Literal["exhaustive", "sequential"] = "exhaustive"
    dense: bool = False  # needs a CUDA build of COLMAP
    # Dense surface on the CPU with OpenMVS (preferred when installed; see openmvs.py).
    openmvs: bool = False
    openmvs_resolution_level: int = 1  # 0 = full resolution, each step halves it
    openmvs_decimate: float = 1.0
    texture: bool = True
    texture_scale: float = 1.0
    step_timeout: float | None = None


@dataclass(slots=True)
class ReconstructionResult:
    mesh_path: Path
    textured: bool
    points_path: Path
    transform: list[list[float]]
    total_images: int
    registered: list[str]
    point_count: int
    points_removed: int
    mean_reprojection_error: float
    mean_track_length: float
    dense: bool
    features: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Problems the user should know about (unused photos, missing texture, …).
    warnings: list[str] = field(default_factory=list)

    @property
    def unregistered(self) -> int:
        return self.total_images - len(self.registered)

    def metrics(self) -> dict[str, Any]:
        return {
            "total_images": self.total_images,
            "registered_images": len(self.registered),
            "sparse_points": self.point_count,
            "points_removed_as_outliers": self.points_removed,
            "mean_reprojection_error_px": round(self.mean_reprojection_error, 4),
            "mean_track_length": round(self.mean_track_length, 3),
            "dense": self.dense,
            "textured": self.textured,
        }


class ReconstructionError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


_NAME = re.compile(r"^\s*Name:\s+(.+?)\s*$")
_FEATURES = re.compile(r"^\s*Features:\s+(\d+)")


class _Run:
    def __init__(
        self,
        runner: ColmapRunner,
        workspace: Path,
        options: ReconstructionOptions,
        progress: ProgressFn,
        cancelled: CancelFn,
    ) -> None:
        self.runner = runner
        self.workspace = workspace
        self.options = options
        self.progress = progress
        self.cancelled = cancelled
        self.log_path = workspace / "colmap.log"
        self.notes: list[str] = []
        self.warnings: list[str] = []

    def step(
        self,
        stage: Stage,
        subcommand: str,
        options: dict[str, str | int | float | bool | Path],
        *,
        total_images: int | None = None,
        on_line: Callable[[str], None] | None = None,
        within: tuple[float, float] = (0.0, 1.0),
    ) -> None:
        """Run one COLMAP step; its own 0–1 progress maps onto ``within`` of the stage."""
        low, high = within

        def report(update: StepProgress) -> None:
            if on_line is not None:
                on_line(update.line)
            if update.fraction is not None:
                self.progress(stage, low + (high - low) * update.fraction, None)

        try:
            self.runner.run(
                subcommand,
                options,
                cwd=self.workspace,
                log_path=self.log_path,
                on_progress=report,
                cancelled=self.cancelled,
                total_images=total_images,
                timeout=self.options.step_timeout,
            )
        except ColmapFailedError as exc:
            raise ReconstructionError(
                "colmap_failed",
                f"COLMAP stopped during {stage} (colmap {subcommand}, exit code {exc.returncode}).",
                {"subcommand": subcommand, "log_tail": exc.tail[-15:]},
            ) from exc


def _to_text(run: _Run, model_dir: Path, name: str) -> Path:
    out = run.workspace / "text" / name
    out.mkdir(parents=True, exist_ok=True)
    run.step(
        "mapping",
        "model_converter",
        {"input_path": model_dir, "output_path": out, "output_type": "TXT"},
    )
    return out


def _largest_model(run: _Run, sparse: Path) -> tuple[Path, SparseModel]:
    best: tuple[Path, SparseModel] | None = None
    for model_dir in sorted(p for p in sparse.iterdir() if p.is_dir()):
        model = read_text_model(_to_text(run, model_dir, model_dir.name))
        if best is None or len(model.images) > len(best[1].images):
            best = (model_dir, model)
    if best is None:
        raise ReconstructionError(NO_START_CODE, NO_START_MESSAGE)
    return best


def _filter_points(model: SparseModel, transform: np.ndarray, out: Path) -> tuple[int, int, str]:
    """Remove isolated points (Open3D statistical outlier removal) and write a PLY."""
    points = model.points @ transform[:3, :3].T + transform[:3, 3]
    colors = model.colors
    removed = 0
    method = "none (Open3D not installed)"
    if importlib.util.find_spec("open3d") is not None and len(points) >= 50:
        import open3d as o3d  # noqa: PLC0415 - optional, heavy

        cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
        _, keep = cloud.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
        keep_arr = np.asarray(keep, dtype=np.int64)
        removed = len(points) - len(keep_arr)
        points, colors = points[keep_arr], colors[keep_arr]
        method = "Open3D statistical outlier removal (20 neighbours, 2 standard deviations)"
    _write_ply(out, points, colors)
    return len(points), removed, method


def _write_ply(path: Path, points: np.ndarray, colors: np.ndarray) -> None:
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {len(points)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n"
    )
    record = np.dtype([("xyz", "<f4", 3), ("rgb", "u1", 3)])
    data = np.empty(len(points), dtype=record)
    data["xyz"] = points
    data["rgb"] = colors
    with path.open("wb") as handle:
        handle.write(header.encode("ascii"))
        handle.write(data.tobytes())


def _listing(names: list[str]) -> str:
    return f"{', '.join(names[:5])}{'…' if len(names) > 5 else ''}"


def _extract_features(run: _Run, images_dir: Path, database: Path, count: int) -> dict[str, int]:
    run.progress("features", 0.0, f"Finding features in {count} photos.")
    features: dict[str, int] = {}
    current: list[str] = []

    def collect(line: str) -> None:
        if match := _NAME.match(line):
            current[:] = [match.group(1)]
        elif (match := _FEATURES.match(line)) and current:
            features[current[0]] = int(match.group(1))

    run.step(
        "features",
        "feature_extractor",
        {
            "database_path": database,
            "image_path": images_dir,
            "ImageReader.single_camera": run.options.single_camera,
            "FeatureExtraction.use_gpu": False,
            "FeatureExtraction.max_image_size": run.options.max_image_size,
            "SiftExtraction.max_num_features": run.options.max_num_features,
        },
        on_line=collect,
    )
    weak = sorted(name for name, found in features.items() if found < LOW_FEATURE_COUNT)
    if weak:
        run.warnings.append(
            f"{len(weak)} photo(s) had few distinctive features (plain, shiny, blurred or dark "
            f"areas): {_listing(weak)}."
        )
    return features


def _match(run: _Run, database: Path) -> None:
    run.progress("matching", 0.0, "Matching features between photos.")
    if run.options.matcher == "sequential":
        run.step(
            "matching",
            "sequential_matcher",
            {
                "database_path": database,
                "FeatureMatching.use_gpu": False,
                "SequentialMatching.overlap": 20,
                "SequentialMatching.quadratic_overlap": True,
            },
        )
    else:
        run.step(
            "matching",
            "exhaustive_matcher",
            {"database_path": database, "FeatureMatching.use_gpu": False},
        )


def _map(
    run: _Run, images_dir: Path, database: Path, names: list[str]
) -> tuple[Path, SparseModel, list[str]]:
    run.progress("mapping", 0.0, "Estimating camera positions and 3D points.")
    sparse = run.workspace / "sparse"
    sparse.mkdir(exist_ok=True)
    try:
        run.step(
            "mapping",
            "mapper",
            {"database_path": database, "image_path": images_dir, "output_path": sparse},
            total_images=len(names),
        )
    except ReconstructionError as exc:
        # COLMAP exits with an error when it finds no pair of photos to start from.
        if any(p.is_dir() for p in sparse.iterdir()):
            raise
        raise ReconstructionError(NO_START_CODE, NO_START_MESSAGE, exc.details) from exc
    model_dir, model = _largest_model(run, sparse)
    registered = sorted(image.name for image in model.images)
    if len(registered) < MIN_REGISTERED:
        raise ReconstructionError(
            "too_few_registered",
            f"Only {len(registered)} of {len(names)} photos could be placed in 3D. The photos "
            "probably overlap too little or show too little texture.",
            {"registered": registered},
        )
    missing = sorted(set(names) - set(registered))
    if missing:
        run.warnings.append(
            f"{len(missing)} of {len(names)} photos could not be placed and were not used: "
            f"{_listing(missing)}."
        )
    if len([p for p in sparse.iterdir() if p.is_dir()]) > 1:
        run.warnings.append(
            "The photos formed separate groups that could not be connected; the largest group "
            "was used. Photos bridging the groups would let all of them be used."
        )
    return model_dir, model, registered


def _surface(run: _Run, images_dir: Path, model_dir: Path, dense_dir: Path) -> Path:
    options = run.options
    if options.dense or options.texture:
        run.progress("meshing", 0.0, "Preparing undistorted images.")
        run.step(
            "meshing",
            "image_undistorter",
            {
                "image_path": images_dir,
                "input_path": model_dir,
                "output_path": dense_dir,
                "max_image_size": options.max_image_size,
            },
            within=(0.0, 0.3),
        )
    mesh_path = run.workspace / "mesh.ply"
    if options.dense:
        run.progress("meshing", 0.3, "Computing dense depth maps (CUDA).")
        run.step(
            "meshing",
            "patch_match_stereo",
            {"workspace_path": dense_dir, "PatchMatchStereo.geom_consistency": True},
            within=(0.3, 0.8),
        )
        run.step(
            "meshing",
            "stereo_fusion",
            {"workspace_path": dense_dir, "output_path": dense_dir / "fused.ply"},
        )
        run.step(
            "meshing",
            "delaunay_mesher",
            {"input_path": dense_dir, "input_type": "dense", "output_path": mesh_path},
        )
    else:
        run.progress("meshing", 0.5, "Building a surface from the sparse points.")
        run.step(
            "meshing",
            "delaunay_mesher",
            {"input_path": model_dir, "input_type": "sparse", "output_path": mesh_path},
        )
        run.notes.append(
            "Surface built from sparse points (no CUDA GPU for dense reconstruction): the shape "
            "is measured but coarse, and thin or untextured parts may be missing."
        )
    if not mesh_path.is_file() or mesh_path.stat().st_size < 200:
        raise ReconstructionError(
            "no_surface",
            "COLMAP placed the cameras but could not build a surface from the points.",
        )
    return mesh_path


def _openmvs_surface(
    run: _Run, images_dir: Path, model_dir: Path, dense_dir: Path
) -> tuple[Path, bool]:
    """Dense points, surface and texture with OpenMVS on the CPU."""
    options = run.options
    run.progress("meshing", 0.0, "Preparing undistorted images.")
    run.step(
        "meshing",
        "image_undistorter",
        {
            "image_path": images_dir,
            "input_path": model_dir,
            "output_path": dense_dir,
            "max_image_size": options.max_image_size,
        },
        within=(0.0, 0.1),
    )
    mvs_dir = run.workspace / "mvs"
    mvs_dir.mkdir(exist_ok=True)
    timeout = options.step_timeout

    def failed(exc: OpenMVSFailedError) -> ReconstructionError:
        return ReconstructionError(
            "openmvs_failed",
            f"OpenMVS stopped ({exc.program}: {exc.message}). The cameras were placed; try "
            "again with Fast quality, or without OpenMVS for a coarse surface.",
            {"program": exc.program, "log_tail": exc.tail[-15:]},
        )

    try:
        mvs = OpenMVSRunner(mvs_dir, log_path=run.log_path)
        scene = mvs.import_colmap(dense_dir, cancelled=run.cancelled, timeout=timeout)
        run.progress("meshing", 0.15, "Computing dense depth maps on the CPU (OpenMVS).")
        dense_scene = mvs.densify(
            scene,
            resolution_level=options.openmvs_resolution_level,
            cancelled=run.cancelled,
            timeout=timeout,
        )
        run.progress("meshing", 0.7, "Building the surface through the dense points.")
        mesh = mvs.mesh(
            dense_scene, decimate=options.openmvs_decimate, cancelled=run.cancelled, timeout=timeout
        )
    except OpenMVSFailedError as exc:
        raise failed(exc) from exc
    run.notes.append(
        "Dense surface computed on the CPU with OpenMVS (depth maps at "
        f"1/{2**options.openmvs_resolution_level} of the photo resolution)."
    )
    if not options.texture:
        return mesh, False
    run.progress("texturing", 0.0, "Projecting the photos onto the surface (OpenMVS).")
    try:
        textured = mvs.texture(dense_scene, mesh, cancelled=run.cancelled, timeout=timeout)
    except OpenMVSFailedError as exc:
        run.warnings.append(f"Texturing failed, so the surface is untextured ({exc.message})")
        return mesh, False
    return textured, True


def _texture(run: _Run, mesh_path: Path, dense_dir: Path) -> tuple[Path, bool]:
    """Texture the surface; a failure keeps the untextured surface."""
    if not run.options.texture:
        return mesh_path, False
    run.progress("texturing", 0.0, "Projecting the photos onto the surface.")
    texture_dir = run.workspace / "textured"
    try:
        run.step(
            "texturing",
            "mesh_texturer",
            {
                "workspace_path": dense_dir,
                "input_path": mesh_path,
                "output_path": texture_dir,
                "MeshTextureMapping.texture_scale_factor": run.options.texture_scale,
            },
        )
    except ReconstructionError as exc:
        run.warnings.append(f"Texturing failed, so the surface is untextured ({exc.message})")
        return mesh_path, False
    textured_mesh = texture_dir / "mesh.ply"
    if textured_mesh.is_file() and (texture_dir / "texture.png").is_file():
        return textured_mesh, True
    run.warnings.append("Texturing produced no texture, so the surface is untextured.")
    return mesh_path, False


def reconstruct(
    images_dir: Path,
    workspace: Path,
    options: ReconstructionOptions,
    *,
    runner: ColmapRunner,
    progress: ProgressFn,
    cancelled: CancelFn,
) -> ReconstructionResult:
    """Run the whole pipeline in ``workspace``. Raises ReconstructionError with a
    message meant for the user when the photos cannot be reconstructed."""
    names = sorted(p.name for p in images_dir.iterdir() if p.is_file())
    if len(names) < MIN_PHOTOS:
        raise ReconstructionError(
            "too_few_photos",
            f"Photogrammetry needs at least {MIN_PHOTOS} photos of the object from different "
            "angles (20 or more for a usable result). It cannot recover shape from one photo.",
        )
    workspace.mkdir(parents=True, exist_ok=True)
    run = _Run(runner, workspace, options, progress, cancelled)
    database = workspace / "database.db"

    features = _extract_features(run, images_dir, database, len(names))
    _match(run, database)
    model_dir, model, registered = _map(run, images_dir, database, names)

    progress("filtering", 0.0, "Orienting the model and removing stray points.")
    transform = upright_transform(estimate_up(model.images, model.points), model.points)
    points_path = workspace / "points.ply"
    kept, removed, method = _filter_points(model, transform, points_path)
    run.notes.append(f"Point filtering: {method}; {removed} of {len(model.points)} removed.")

    dense_dir = workspace / "dense"
    if options.openmvs:
        mesh_path, textured = _openmvs_surface(run, images_dir, model_dir, dense_dir)
    else:
        mesh_path, textured = _texture(
            run, _surface(run, images_dir, model_dir, dense_dir), dense_dir
        )
    progress("texturing", 1.0, None)
    shutil.rmtree(dense_dir / "stereo", ignore_errors=True)

    return ReconstructionResult(
        mesh_path=mesh_path,
        textured=textured,
        points_path=points_path,
        transform=transform.tolist(),
        total_images=len(names),
        registered=registered,
        point_count=kept,
        points_removed=removed,
        mean_reprojection_error=model.mean_reprojection_error,
        mean_track_length=model.mean_track_length,
        dense=options.dense or options.openmvs,
        features=features,
        notes=run.notes,
        warnings=run.warnings,
    )
