"""Geometry and texture processing for the Enhance workflow.

Each operation works on a ``trimesh.Scene`` loaded from a version's GLB and
reports what it did — or why it did nothing — as a :class:`Step`. Nothing is
claimed that was not done: operations that need tools this installation does
not have are reported as ``unavailable``, and operations that would damage a
textured model (filling holes, plain decimation) are skipped on textured models
instead of silently discarding the texture.

Textured models over the Professional triangle budget are reduced in Blender
instead: a reduced copy gets new UVs and the original's base colour is baked
onto it (see :func:`bake_reduce`). Without Blender they are kept as they are.

Textured meshes duplicate vertices along UV seams; topology (pieces, normals,
holes) is therefore computed on a position-merged copy whose faces keep the
original order, and the result is applied back to the original faces.
"""

import logging
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import trimesh
from PIL import Image

from ai3d.models.enums import QualityMode
from ai3d.processing import quads
from ai3d.processing.mesh import duplicate_face_count, load_model_scene
from ai3d_engine.blender import (
    BlenderFailedError,
    BlenderNotFoundError,
    CancelCheck,
    check_blender,
    run_blender,
)
from ai3d_engine.local_ai import engine as local_ai_engine

log = logging.getLogger(__name__)

StepStatus = Literal["applied", "skipped", "unavailable"]
# Entered around Blender runs: waits for the computer's heavy-work slot.
HeavyTurn = Callable[[], AbstractContextManager[None]]
Axis = Literal["x", "y", "z", "longest"]


@dataclass(slots=True)
class Step:
    code: str
    title: str
    status: StepStatus
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RemeshSettings:
    """Rebuild the surface in Blender: an even quad layout (QuadriFlow) or voxels."""

    method: Literal["quadriflow", "voxel"]
    target_faces: int = 20_000  # QuadriFlow: about this many quads
    resolution: int = 300  # voxel: cells across the model's diagonal
    maps: tuple[str, ...] = ()  # extra maps to bake: "normal", "occlusion"


@dataclass(frozen=True, slots=True)
class ScaleReference:
    """A real measurement of the object, e.g. "its height (y) is 120 mm"."""

    axis: Axis
    length_mm: float


@dataclass(frozen=True, slots=True)
class Profile:
    min_part_fraction: float
    fix_normals: bool
    fill_holes: bool
    max_texture: int
    decimate_to: int | None
    report_tools: bool  # list retopology / baking (Professional)


# Baked texture size: the original's largest base-colour texture, within these limits.
# Baking runs on the CPU; 4096 px keeps it to minutes for 300k-triangle results.
BAKE_MIN_TEXTURE = 1024
BAKE_MAX_TEXTURE = 4096
BAKE_TIMEOUT = 3600.0  # seconds
# A remeshed surface is accepted only when it stays this close to the original
# (distances both ways, as a fraction of the bounding-box diagonal).
REMESH_MAX_MEAN = 0.003
REMESH_MAX_P95 = 0.01
REMESH_TIMEOUT = 3600.0


PROFILES: dict[QualityMode, Profile] = {
    QualityMode.FAST: Profile(0.001, False, False, 2048, None, False),
    QualityMode.HIGH_QUALITY: Profile(0.005, True, True, 4096, None, False),
    QualityMode.PROFESSIONAL: Profile(0.005, True, True, 8192, 300_000, True),
}


@dataclass(slots=True)
class ProcessingResult:
    scene: trimesh.Scene
    steps: list[Step] = field(default_factory=list)
    scale: dict[str, Any] | None = None

    @property
    def changed(self) -> bool:
        return any(step.status == "applied" for step in self.steps)


# -- helpers --------------------------------------------------------------------------


def _meshes(scene: trimesh.Scene) -> list[tuple[str, trimesh.Trimesh]]:
    return [(n, g) for n, g in scene.geometry.items() if isinstance(g, trimesh.Trimesh)]


def _shape(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Position-only copy (texture seams merged); faces keep their order and count."""
    shape = trimesh.Trimesh(vertices=mesh.vertices.copy(), faces=mesh.faces.copy(), process=False)
    shape.merge_vertices(merge_tex=True, merge_norm=True)
    return shape


def _textured(mesh: trimesh.Trimesh) -> bool:
    return mesh.visual is not None and mesh.visual.kind == "texture"


def _face_count(scene: trimesh.Scene) -> int:
    return sum(len(g.faces) for _, g in _meshes(scene))


def _keep_faces(mesh: trimesh.Trimesh, mask: np.ndarray) -> int:
    removed = int((~mask).sum())
    if removed:
        mesh.update_faces(mask)
        mesh.remove_unreferenced_vertices()
    return removed


def _component_labels(mesh: trimesh.Trimesh) -> np.ndarray:
    shape = _shape(mesh)
    return np.asarray(
        trimesh.graph.connected_component_labels(  # type: ignore[no-untyped-call]
            shape.face_adjacency, node_count=len(shape.faces)
        )
    )


def _world_vertices(scene: trimesh.Scene) -> list[tuple[str, trimesh.Trimesh, np.ndarray]]:
    """Each mesh instance with its vertices in scene (world) coordinates."""
    out = []
    for node in scene.graph.nodes_geometry:
        transform, name = scene.graph[node]
        mesh = scene.geometry.get(name)
        if isinstance(mesh, trimesh.Trimesh):
            out.append((str(name), mesh, trimesh.transform_points(mesh.vertices, transform)))
    return out


# -- operations -----------------------------------------------------------------------


def cleanup(scene: trimesh.Scene) -> Step:
    """Remove degenerate and duplicate triangles and unused vertices."""
    removed = 0
    for _, mesh in _meshes(scene):
        shape = _shape(mesh)
        keep = shape.nondegenerate_faces()
        _, first = duplicate_face_count(shape.faces, len(shape.vertices))
        unique = np.zeros(len(shape.faces), dtype=bool)
        unique[first] = True
        removed += _keep_faces(mesh, keep & unique)
    if removed:
        return Step(
            "cleanup", "Clean-up", "applied", f"Removed {removed:,} degenerate/duplicate triangles."
        )
    return Step("cleanup", "Clean-up", "skipped", "No degenerate or duplicate triangles found.")


def remove_small_parts(scene: trimesh.Scene, min_fraction: float) -> Step:
    """Drop loose pieces smaller than ``min_fraction`` of the model's triangles."""
    total = _face_count(scene)
    limit = max(int(total * min_fraction), 1)
    removed_faces = removed_parts = 0
    for _, mesh in _meshes(scene):
        labels = _component_labels(mesh)
        if len(labels) == 0:
            continue
        sizes = np.bincount(labels)
        small = sizes < limit
        if small.all():  # never delete a whole mesh this way
            small[np.argmax(sizes)] = False
        removed_parts += int(small.sum())
        removed_faces += _keep_faces(mesh, ~small[labels])
    title = "Remove loose fragments"
    if removed_parts:
        return Step(
            "small_parts",
            title,
            "applied",
            f"Removed {removed_parts:,} piece(s) under {limit:,} triangles "
            f"({removed_faces:,} triangles in total).",
        )
    return Step("small_parts", title, "skipped", f"No pieces under {limit:,} triangles.")


def fix_normals(scene: trimesh.Scene) -> Step:
    """Make triangle winding consistent (and outward on closed pieces)."""
    flipped = 0
    for _, mesh in _meshes(scene):
        shape = _shape(mesh)
        before = shape.faces.copy()
        trimesh.repair.fix_normals(shape, multibody=True)  # type: ignore[no-untyped-call]
        # A flipped triangle keeps its vertex set but reverses its order.
        changed = np.any(before != shape.faces, axis=1)
        if changed.any():
            faces = mesh.faces.copy()
            faces[changed] = faces[changed][:, ::-1]
            mesh.faces = faces
            flipped += int(changed.sum())
    title = "Consistent normals"
    if flipped:
        return Step(
            "normals", title, "applied", f"Flipped {flipped:,} triangles that faced inwards."
        )
    return Step("normals", title, "skipped", "Triangle winding was already consistent.")


def fill_holes(scene: trimesh.Scene) -> Step:
    """Close small holes on uncoloured, untextured meshes.

    New triangles would have no texture coordinates or colours, so meshes with a
    texture or per-face/vertex colours are left alone (that needs texture baking).
    """
    filled = skipped = 0
    for name, mesh in _meshes(scene):
        visual = mesh.visual
        if visual is not None and visual.kind in {"texture", "face", "vertex"}:
            skipped += 1
            continue
        before = len(mesh.faces)
        shape = _shape(mesh)
        trimesh.repair.fill_holes(shape)
        if len(shape.faces) > before:
            material = getattr(visual, "material", None)
            if material is not None:  # keep the plain PBR material (colour, roughness…)
                shape.visual = trimesh.visual.TextureVisuals(material=material)  # type: ignore[no-untyped-call]
            scene.geometry[name] = shape
            filled += len(shape.faces) - before
    title = "Fill holes"
    if filled:
        return Step("holes", title, "applied", f"Added {filled:,} triangles to close small holes.")
    if skipped:
        return Step(
            "holes",
            title,
            "skipped",
            "Not done on textured or coloured meshes: new triangles would have no texture or "
            "colour (that needs texture baking).",
        )
    return Step("holes", title, "skipped", "No small holes that could be closed.")


@dataclass(frozen=True, slots=True)
class UpscaleLimits:
    """Limits for AI texture upscaling (from settings; see Settings.upscale_*)."""

    # Measured on a 6-core Intel CPU: 1024 px → 2048 px in 17 s, 2048 px → 4096 px in
    # about a minute; a 4096 px input takes about five.
    max_input_px: int = 4096
    tile_px: int = 192
    # Free memory when the job starts; None = not checked.
    available_bytes: int | None = None


# Engine and model, plus PyTorch's working memory for one tile; the rest scales with
# the image. Measured peaks: 319 MB for 512 → 2048 px, 326 MB for 1024 → 2048 px; the
# estimate is about twice that, so a texture is only tried with a safe margin.
_UPSCALE_BASE_BYTES = 600 * 1024 * 1024


def upscale_memory_bytes(width: int, height: int, out_width: int, out_height: int) -> int:
    """Rough peak memory to upscale one texture: input as float32 RGB, output as bytes
    (array and image), plus the fixed base."""
    return _UPSCALE_BASE_BYTES + width * height * 3 * 4 + out_width * out_height * 3 * 2


UPSCALE_DETAIL = (
    "AI upscaling makes textures look sharper by inventing plausible fine detail; it does "
    "not recover detail the original texture never had."
)


def _upscale_skip(
    image: Image.Image, scale: int, max_size: int, limits: UpscaleLimits
) -> tuple[str, str] | None:
    """Why a texture is not upscaled: ("", …) big enough already, ("large", size) over
    the input limit, ("short", text) not enough free memory; None to upscale it."""
    size = f"{image.width}×{image.height}"
    if max(image.size) >= max_size:
        return ("", size)
    if max(image.size) > limits.max_input_px:
        return ("large", size)
    factor = min(scale, max_size / max(image.size))
    out_w, out_h = round(image.width * factor), round(image.height * factor)
    need = upscale_memory_bytes(image.width, image.height, out_w, out_h)
    if limits.available_bytes is not None and need > limits.available_bytes:
        return (
            "short",
            f"{size} needs about {need / 1024**3:.1f} GB, "
            f"{limits.available_bytes / 1024**3:.1f} GB is free",
        )
    return None


def upscale_textures(
    scene: trimesh.Scene,
    scale: int,
    max_size: int,
    *,
    workdir: Path | None = None,
    cancel: CancelCheck | None = None,
    limits: UpscaleLimits | None = None,
) -> Step:
    """Upscale base-colour textures with the local AI engine (Real-ESRGAN compact).

    Each distinct texture smaller than ``max_size`` is enlarged ×``scale`` (capped at
    ``max_size``); UVs are unaffected. Raises LocalAICancelledError when cancelled.
    """
    title = f"AI texture upscaling (×{scale})"
    problem = local_ai_engine.upscaler_problem()
    if problem is not None:
        return Step("upscale", title, "unavailable", problem)
    limits = limits or UpscaleLimits()
    done: list[str] = []
    large: list[str] = []
    short: list[str] = []
    seen: set[int] = set()
    with tempfile.TemporaryDirectory(prefix="upscale-", dir=workdir) as tmp:
        root = Path(tmp)
        for _, mesh in _meshes(scene):
            material = getattr(mesh.visual, "material", None)
            for attr in ("baseColorTexture", "image"):
                image = getattr(material, attr, None) if material is not None else None
                if not isinstance(image, Image.Image) or id(image) in seen:
                    continue
                seen.add(id(image))
                reason = _upscale_skip(image, scale, max_size, limits)
                if reason is not None:
                    if reason[0]:
                        (large if reason[0] == "large" else short).append(reason[1])
                    continue
                n = len(seen)
                source, output = root / f"in-{n}.png", root / f"out-{n}.png"
                image.save(source, format="PNG")
                try:
                    local_ai_engine.upscale(
                        source,
                        output,
                        root / f"run-{n}",
                        scale=scale,
                        max_size=max_size,
                        tile=limits.tile_px,
                        cancel=cancel,
                    )
                except local_ai_engine.LocalAIFailedError as exc:
                    return Step("upscale", title, "skipped", f"{exc.message} Textures were kept.")
                new = Image.open(output)
                new.load()
                setattr(material, attr, new)
                done.append(f"{image.width}×{image.height} → {new.width}×{new.height}")
    return _upscale_step(title, done, large, short, limits, seen=bool(seen), max_size=max_size)


def _upscale_step(
    title: str,
    done: list[str],
    large: list[str],
    short: list[str],
    limits: UpscaleLimits,
    *,
    seen: bool,
    max_size: int,
) -> Step:
    kept = []
    if large:
        kept.append(
            f"Textures over {limits.max_input_px} px ({', '.join(large)}) are already detailed "
            "and would take too long on a CPU; they were kept."
        )
    if short:
        kept.append(
            f"Not enough free memory ({'; '.join(short)}): close other programs and try "
            "again. Those textures were kept."
        )
    if not done:
        if not seen:
            detail = "No texture to upscale."
        elif kept:
            detail = " ".join(kept)
        else:
            detail = f"Textures are already {max_size} px or larger."
        return Step("upscale", title, "skipped", detail)
    return Step(
        "upscale",
        title,
        "applied",
        " ".join([f"Real-ESRGAN (on this computer): {', '.join(done)}. {UPSCALE_DETAIL}", *kept]),
    )


def limit_textures(scene: trimesh.Scene, max_size: int) -> Step:
    """Downscale textures larger than ``max_size`` (UVs are unaffected)."""
    resized: list[str] = []
    seen: set[int] = set()
    for _, mesh in _meshes(scene):
        material = getattr(mesh.visual, "material", None)
        if material is None:
            continue
        for attr in (
            "baseColorTexture",
            "metallicRoughnessTexture",
            "normalTexture",
            "emissiveTexture",
            "occlusionTexture",
            "image",
        ):
            image = getattr(material, attr, None)
            if not isinstance(image, Image.Image) or id(image) in seen:
                continue
            seen.add(id(image))
            if max(image.size) <= max_size:
                continue
            scale = max_size / max(image.size)
            new = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
            resized.append(f"{image.width}×{image.height} → {new.width}×{new.height}")
            setattr(material, attr, new)
    title = "Texture size"
    if resized:
        return Step("textures", title, "applied", "Downscaled " + ", ".join(resized) + ".")
    detail = f"All textures are within {max_size} px." if seen else "The model has no textures."
    return Step("textures", title, "skipped", detail)


def decimate(scene: trimesh.Scene, target_faces: int) -> Step:
    """Quadric decimation to a triangle budget (untextured meshes only).

    Textured models are left alone here; :func:`bake_reduce` handles them.
    """
    title = f"Reduce to {target_faces:,} triangles"
    total = _face_count(scene)
    if total <= target_faces:
        return Step("decimate", title, "skipped", f"Already {total:,} triangles.")
    meshes = _meshes(scene)
    if any(_textured(mesh) for _, mesh in meshes):
        return Step(
            "decimate",
            title,
            "skipped",
            "Not done on textured models: decimation would discard the texture mapping "
            "(re-texturing needs texture baking in Blender).",
        )
    for name, mesh in meshes:
        share = max(int(target_faces * len(mesh.faces) / total), 4)
        reduced = mesh.simplify_quadric_decimation(face_count=share)
        reduced.visual = mesh.visual.copy() if mesh.visual is not None else reduced.visual
        scene.geometry[name] = reduced
    return Step("decimate", title, "applied", f"{total:,} → {_face_count(scene):,} triangles.")


def _base_texture_size(scene: trimesh.Scene) -> int:
    sizes = [max(image.size) for _, m in _meshes(scene) if (image := _base_image(m)) is not None]
    return max(sizes, default=BAKE_MIN_TEXTURE)


def _base_image(mesh: trimesh.Trimesh) -> Image.Image | None:
    material = getattr(mesh.visual, "material", None)
    for attr in ("baseColorTexture", "image"):
        image = getattr(material, attr, None) if material is not None else None
        if isinstance(image, Image.Image):
            return image
    return None


def bake_texture_size(scene: trimesh.Scene, max_texture: int) -> int:
    """Power of two near the original's texture size, within the bake limits."""
    upper = min(max_texture, BAKE_MAX_TEXTURE)
    wanted = min(max(_base_texture_size(scene), BAKE_MIN_TEXTURE), upper)
    return min(int(2 ** round(np.log2(wanted))), upper)


def bake_reduce(
    scene: trimesh.Scene,
    target_faces: int,
    texture_size: int,
    *,
    workdir: Path | None = None,
    cancel: CancelCheck | None = None,
) -> tuple[trimesh.Scene | None, Step, Step]:
    """Reduce a textured model in Blender and bake its base colour onto new UVs.

    Blender joins the meshes into one copy, decimates it (collapse) to
    ``target_faces``, unwraps it (Smart UV Project) and bakes the original's
    base colour onto it with Cycles (selected-to-active; the cage distance comes
    from the measured distance between the original and the copy). Returns the
    new scene (or None when nothing was done) and the decimate and baking steps.
    Raises BlenderCancelledError when ``cancel`` asks to stop.
    """
    reduce_title = f"Reduce to {target_faces:,} triangles"
    bake_title = "Texture baking"
    total = _face_count(scene)
    with tempfile.TemporaryDirectory(prefix="bake-", dir=workdir) as tmp:
        root = Path(tmp)
        source, output = root / "source.glb", root / "baked.glb"
        source.write_bytes(scene.export(file_type="glb"))  # type: ignore[no-untyped-call]
        try:
            info = run_blender(
                "bake",
                {
                    "input": str(source),
                    "output": str(output),
                    "texture": str(root / "basecolor.png"),
                    "target_triangles": target_faces,
                    "texture_size": texture_size,
                },
                workdir=root,
                timeout=BAKE_TIMEOUT,
                cancel=cancel,
                poll_seconds=2.0,
            )
            baked = load_model_scene(output)
        except (BlenderFailedError, BlenderNotFoundError) as exc:
            reason = exc.message if isinstance(exc, BlenderFailedError) else str(exc)
            log.warning("bake failed", extra={"reason": reason})
            detail = (
                f"Blender could not reduce and bake the model ({reason}); it was kept as it was."
            )
            return (
                None,
                Step("decimate", reduce_title, "skipped", detail),
                Step("baking", bake_title, "skipped", detail),
            )
    meshes = _meshes(baked)
    after = _face_count(baked)
    if not meshes or not all(_textured(m) and _base_image(m) is not None for _, m in meshes):
        detail = "Blender's result had no baked texture, so the model was kept as it was."
        return (
            None,
            Step("decimate", reduce_title, "skipped", detail),
            Step("baking", bake_title, "skipped", detail),
        )
    seconds = info["seconds"]
    unsupported = info.get("materials_unsupported") or []
    bake_detail = (
        f"Baked the original's base colour onto new UVs: {texture_size}×{texture_size} px, "
        f"{seconds['bake']:.1f} s on the CPU (Cycles; {seconds['total']:.0f} s in Blender in "
        f"total). Metallic {info['metallic']:.2g} and roughness {info['roughness']:.2g} are kept "
        "as plain values; normal, metallic-roughness and emissive maps are not transferred."
    )
    if unsupported:
        bake_detail += f" Materials not baked (no Principled BSDF): {', '.join(unsupported)}."
    return (
        baked,
        Step(
            "decimate",
            reduce_title,
            "applied",
            f"{total:,} → {after:,} triangles in Blender (collapse decimation; the "
            f"surface differs from the original by up to {info['max_deviation']:.3g} units).",
        ),
        Step("baking", bake_title, "applied", bake_detail),
    )


# Share of the height, from the bottom, searched for the supporting plane.
GROUND_SEARCH_BAND = 0.2
# The plane must be at least this wide relative to the model (horizontally), and at most
# this share of the model may lie below it.
GROUND_MIN_WIDTH = 0.5
GROUND_MAX_BELOW = 0.03


def _not_ground(
    points: np.ndarray,
    sample: np.ndarray,
    normal: np.ndarray,
    offset: float,
    *,
    threshold: float,
    bounds: tuple[np.ndarray, np.ndarray],
) -> str | None:
    """Why the plane is not a table or board under the object, or None when it is."""
    lo, hi = bounds
    height = -offset / normal[1]  # y of the plane at x = z = 0 (normal is near-vertical)
    if height > lo[1] + 0.25 * (hi[1] - lo[1]):
        return "The largest flat area is not at the bottom."
    # A table or board spreads under the object; a curved bottom only touches a small patch.
    on_plane = sample[np.abs(sample @ normal + offset) < threshold]
    footprint = float(np.linalg.norm(np.ptp(on_plane[:, [0, 2]], axis=0)))
    if footprint < GROUND_MIN_WIDTH * float(np.linalg.norm((hi - lo)[[0, 2]])):
        return "The flat area at the bottom is too small to be a table."
    # Under a table there is only reconstruction noise, never part of the object.
    below = float(((points @ normal + offset) < -2 * threshold).mean())
    if below > GROUND_MAX_BELOW:
        return f"{below:.0%} of the model lies under the flat area, so it is not a table."
    return None


def remove_ground(scene: trimesh.Scene, seed: int = 0) -> Step:
    """Remove a horizontal plane under the object (e.g. the table in a photogrammetry scan).

    RANSAC finds the dominant plane; it must be roughly horizontal, near the bottom
    and large among the lowest points. Triangles lying on it are removed, then only the
    largest remaining piece is kept (the object).
    """
    title = "Remove ground / table"
    instances = _world_vertices(scene)
    if not instances:
        return Step("ground", title, "skipped", "The model has no geometry.")
    points = np.concatenate([v for _, _, v in instances])
    lo, hi = points.min(axis=0), points.max(axis=0)
    diagonal = float(np.linalg.norm(hi - lo)) or 1.0
    threshold = diagonal * 0.01
    rng = np.random.default_rng(seed)
    # The ground is at the bottom: search among the lowest points, where a thin board or
    # table top is the majority (on a dense scan of a whole object it is only a few %).
    bottom = points[points[:, 1] <= lo[1] + GROUND_SEARCH_BAND * (hi[1] - lo[1])]
    pool = bottom if len(bottom) >= 100 else points
    sample = pool[rng.choice(len(pool), min(len(pool), 20_000), replace=False)]
    best: tuple[int, np.ndarray, float] | None = None
    for _ in range(300):
        a, b, c = sample[rng.choice(len(sample), 3, replace=False)]
        normal = np.cross(b - a, c - a)
        norm = np.linalg.norm(normal)
        if norm < 1e-12:
            continue
        normal /= norm
        if abs(normal[1]) < 0.9:  # only (near-)horizontal planes
            continue
        offset = -float(normal @ a)
        inliers = int((np.abs(sample @ normal + offset) < threshold).sum())
        if best is None or inliers > best[0]:
            best = (inliers, normal, offset)
    if best is None or best[0] < 0.15 * len(sample):
        return Step("ground", title, "skipped", "No large horizontal plane found under the model.")
    _, normal, offset = best
    if normal[1] < 0:  # make "above the plane" positive
        normal, offset = -normal, -offset
    problem = _not_ground(points, sample, normal, offset, threshold=threshold, bounds=(lo, hi))
    if problem is not None:
        return Step("ground", title, "skipped", problem)

    removed = 0
    for name, mesh, world in instances:
        # On the plane, or under it: nothing of an object resting on a surface lies below it
        # (reconstruction noise under the table does).
        ground = (world @ normal + offset) < threshold * 2
        removed += _keep_faces(mesh, ~ground[mesh.faces].all(axis=1))
        scene.geometry[name] = mesh
    # Keep only the object: the largest piece left.
    for _, mesh in _meshes(scene):
        labels = _component_labels(mesh)
        if len(labels):
            largest = np.argmax(np.bincount(labels))
            removed += _keep_faces(mesh, labels == largest)
    if not removed:
        return Step("ground", title, "skipped", "Nothing lay on the ground plane.")
    return Step(
        "ground",
        title,
        "applied",
        f"Removed {removed:,} triangles of the supporting surface and loose surroundings.",
    )


def apply_scale(scene: trimesh.Scene, reference: ScaleReference) -> tuple[Step, dict[str, Any]]:
    """Scale the model so that the given extent matches a real measurement (metres)."""
    extents = scene.extents
    index = {"x": 0, "y": 1, "z": 2}.get(reference.axis, int(np.argmax(extents)))
    current = float(extents[index])
    if current <= 0:
        raise ValueError("The model has no extent along that axis.")
    target = reference.length_mm / 1000.0
    factor = target / current
    scene.apply_scale(factor)  # type: ignore[no-untyped-call]
    axis_name = "xyz"[index]
    info = {
        "known": True,
        "axis": axis_name,
        "length_mm": reference.length_mm,
        "factor": factor,
        "source": "user measurement",
    }
    step = Step(
        "scale",
        "Real-world scale",
        "applied",
        f"Scaled ×{factor:.4g} so the {axis_name.upper()} extent is {reference.length_mm:g} mm "
        "(from your measurement; the result is only as accurate as that measurement).",
    )
    return step, info


SmoothingMethod = Literal["sharp", "taubin"]

# Bilateral normal filtering: how different two face normals may be and still be
# averaged (|n1 - n2|, Gaussian width). Across a 90° edge |n1 - n2| ≈ 1.41, which
# gets a weight of ~3e-4: edges and corners stay. Chosen on noisy test shapes (a
# cube, a cylinder, a sphere): 0.2 leaves noise, 0.5 starts rounding edges after
# many passes (docs/BENCHMARKS.md).
SHARP_NORMAL_SIGMA = 0.35
# Pairs of faces processed at a time when weighting (bounds temporary memory).
_PAIR_CHUNK = 2_000_000


def _face_normals(vertices: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    corners = vertices[faces]
    cross = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    double_area = np.linalg.norm(cross, axis=1)
    normals = cross / np.maximum(double_area, 1e-30)[:, None]
    return normals, double_area / 2


def _sharp_smooth(vertices: np.ndarray, faces: np.ndarray, passes: int) -> np.ndarray:
    """Feature-preserving smoothing (bilateral normal filtering, Zheng et al. 2011).

    Each face normal becomes a weighted mean of the normals of faces sharing a
    vertex with it: near faces count more, and faces whose normal differs a lot
    (the other side of an edge) hardly at all. The vertices are then moved to fit
    the filtered normals (Sun et al. 2007). Noise goes; edges and corners stay.
    """
    from scipy import sparse  # noqa: PLC0415 - only needed here

    count = len(vertices)
    face_count = len(faces)
    incidence = sparse.csr_matrix(
        (
            np.ones(face_count * 3, dtype=np.float32),
            (np.repeat(np.arange(face_count, dtype=np.int32), 3), faces.ravel().astype(np.int32)),
        ),
        shape=(face_count, count),
    )
    neighbours = (incidence @ incidence.T).tocoo()
    del incidence
    rows = neighbours.row.astype(np.int32)
    cols = neighbours.col.astype(np.int32)
    del neighbours
    normals, area = _face_normals(vertices, faces)
    centres = vertices[faces].mean(axis=1)
    spatial = np.empty(len(rows), dtype=np.float64)
    gaps = 0.0
    for start in range(0, len(rows), _PAIR_CHUNK):
        r, c = rows[start : start + _PAIR_CHUNK], cols[start : start + _PAIR_CHUNK]
        distance = np.linalg.norm(centres[r] - centres[c], axis=1)
        spatial[start : start + _PAIR_CHUNK] = distance
        gaps += float(distance.sum())
    sigma = gaps / max(len(rows) - face_count, 1) or 1.0  # mean gap between neighbours
    for start in range(0, len(rows), _PAIR_CHUNK):
        d = spatial[start : start + _PAIR_CHUNK]
        spatial[start : start + _PAIR_CHUNK] = area[cols[start : start + _PAIR_CHUNK]] * np.exp(
            -(d * d) / (2 * sigma * sigma)
        )
    weights = np.empty_like(spatial)
    for _ in range(passes):
        for start in range(0, len(rows), _PAIR_CHUNK):
            r, c = rows[start : start + _PAIR_CHUNK], cols[start : start + _PAIR_CHUNK]
            difference = 2.0 - 2.0 * np.einsum("ij,ij->i", normals[r], normals[c])
            weights[start : start + _PAIR_CHUNK] = spatial[start : start + _PAIR_CHUNK] * np.exp(
                -difference / (2 * SHARP_NORMAL_SIGMA**2)
            )
        filtered = sparse.csr_matrix((weights, (rows, cols)), shape=(face_count, face_count))
        normals = filtered @ normals
        normals /= np.maximum(np.linalg.norm(normals, axis=1), 1e-30)[:, None]
    del rows, cols, spatial, weights
    moved = vertices.astype(np.float64).copy()
    valence = np.maximum(np.bincount(faces.ravel(), minlength=count), 1).astype(np.float64)
    for _ in range(passes + 10):
        corners = moved[faces]
        offset = np.einsum("fj,fkj->fk", normals, corners.mean(axis=1)[:, None, :] - corners)
        step = np.zeros_like(moved)
        for k in range(3):
            push = normals * offset[:, k : k + 1]
            for axis in range(3):
                step[:, axis] += np.bincount(faces[:, k], weights=push[:, axis], minlength=count)
        moved += step / valence[:, None]
    return moved


# Scene metadata key: quad pairs from retopology ({geometry name: (P, 2) face indices}).
QUAD_PAIRS = "quad_pairs"

# Working memory of feature-preserving smoothing per triangle, beyond the loaded
# model (measured: 10 million triangles peaked 6.3 GB above loading; see
# docs/BENCHMARKS.md), with some margin.
SHARP_SMOOTHING_BYTES_PER_TRIANGLE = 700


def smooth(
    scene: trimesh.Scene,
    iterations: int,
    method: SmoothingMethod = "sharp",
    *,
    available_bytes: int | None = None,
) -> Step:
    """Smooth the surface; ``iterations`` sets the strength (3, 10, 30 in the app).

    ``sharp`` (default): bilateral normal filtering, which removes noise but keeps
    sharp edges and corners (see :func:`_sharp_smooth`). ``taubin``: Taubin smoothing
    (alternating shrink and inflate), which rounds edges. Both keep the volume.

    Both run on the shape with texture seams merged, then move every original
    vertex with its merged position, so UVs, colours and seams are untouched.
    """
    title = "Smoothing"
    if iterations <= 0:
        return Step("smoothing", title, "skipped", "Not requested.")
    if method == "sharp" and available_bytes is not None:
        need = _face_count(scene) * SHARP_SMOOTHING_BYTES_PER_TRIANGLE
        if need > available_bytes:
            return Step(
                "smoothing",
                title,
                "skipped",
                f"Keeping sharp edges on {_face_count(scene):,} triangles needs about "
                f"{need / 1024**3:.1f} GB of memory and {available_bytes / 1024**3:.1f} GB is "
                "free. Close other programs, or choose Round them off (much lighter).",
            )
    diagonal = float(np.linalg.norm(scene.extents)) or 1.0
    moved: list[np.ndarray] = []
    volume_before = volume_after = 0.0
    closed = True
    for _, mesh in _meshes(scene):
        unique, inverse = trimesh.grouping.unique_rows(mesh.vertices)  # type: ignore[no-untyped-call]
        shape = trimesh.Trimesh(
            vertices=mesh.vertices[unique], faces=inverse[mesh.faces], process=False
        )
        closed = closed and shape.is_watertight
        if closed:
            volume_before += abs(float(shape.volume))
        if method == "taubin":
            trimesh.smoothing.filter_taubin(shape, lamb=0.5, nu=0.53, iterations=iterations)  # type: ignore[no-untyped-call]
        else:
            shape.vertices = _sharp_smooth(
                np.asarray(shape.vertices), np.asarray(shape.faces), iterations
            )
        if closed:
            volume_after += abs(float(shape.volume))
        new = np.asarray(shape.vertices)[inverse]
        moved.append(np.linalg.norm(new - mesh.vertices, axis=1) / diagonal)
        mesh.vertices = new
    if not moved:
        return Step("smoothing", title, "skipped", "No triangle meshes to smooth.")
    distance = np.concatenate(moved)
    how = (
        "keeps sharp edges and corners" if method == "sharp" else "Taubin, which rounds sharp edges"
    )
    volume = (
        f" Volume changed by {100 * (volume_after / volume_before - 1):+.2f}%."
        if closed and volume_before > 0
        else ""
    )
    return Step(
        "smoothing",
        title,
        "applied",
        f"{iterations} passes ({how}): vertices moved {distance.mean():.2%} of the model's "
        f"size on average, at most {distance.max():.2%}.{volume}",
    )


def _remesh_title(settings: RemeshSettings) -> str:
    return (
        f"Retopology (QuadriFlow, about {settings.target_faces:,} quads)"
        if settings.method == "quadriflow"
        else f"Remesh (voxels, {settings.resolution} across)"
    )


def remesh(
    scene: trimesh.Scene,
    settings: RemeshSettings,
    *,
    texture_size: int,
    workdir: Path | None = None,
    cancel: CancelCheck | None = None,
) -> tuple[trimesh.Scene | None, list[Step]]:
    """Rebuild the surface in Blender; keep it only if it stays close to the original.

    Vertex colours are carried over from the nearest point of the original; a
    texture (and any requested normal / occlusion map) is baked onto new UVs.
    Raises BlenderCancelledError when ``cancel`` asks to stop.
    """
    title = _remesh_title(settings)
    maps_title = "Normal / occlusion maps"
    problem = check_blender()
    if problem is not None:
        return None, [Step("retopology", title, "unavailable", problem)]
    with tempfile.TemporaryDirectory(prefix="remesh-", dir=workdir) as tmp:
        root = Path(tmp)
        source, output = root / "source.glb", root / "remeshed.glb"
        source.write_bytes(scene.export(file_type="glb"))  # type: ignore[no-untyped-call]
        request: dict[str, Any] = {
            "input": str(source),
            "output": str(output),
            "method": settings.method,
            "target_faces": settings.target_faces,
            "resolution": settings.resolution,
            "maps": list(settings.maps),
            "texture_size": texture_size,
            "quads_output": str(root / "quads.npy"),
        }
        try:
            info = run_blender(
                "remesh", request, workdir=root, timeout=REMESH_TIMEOUT, cancel=cancel
            )
            result = load_model_scene(output)
            corners_path = root / "quads.npy"
            if corners_path.exists():
                pairs = quads.pair_triangles(result, np.load(corners_path))
                if pairs:
                    result.metadata[QUAD_PAIRS] = pairs
        except (BlenderFailedError, BlenderNotFoundError) as exc:
            reason = exc.message if isinstance(exc, BlenderFailedError) else str(exc)
            log.warning("remesh failed", extra={"reason": reason})
            return None, [
                Step("retopology", title, "skipped", f"{reason} The model was kept as it was.")
            ]
    fidelity = info["fidelity"]
    distances = (
        f"the new surface is within {fidelity['p95']:.2%} of the model's size of the original "
        f"for 95% of points (mean {fidelity['mean']:.2%}, worst {fidelity['max']:.2%})"
    )
    if fidelity["p95"] > REMESH_MAX_P95 or fidelity["mean"] > REMESH_MAX_MEAN:
        return None, [
            Step(
                "retopology",
                title,
                "skipped",
                f"Not kept: {distances}, more than the {REMESH_MAX_P95:.0%} allowed. The model "
                "was kept as it was; a higher face count or resolution follows it more closely.",
            )
        ]
    paired = sum(len(p) for p in result.metadata.get(QUAD_PAIRS, {}).values())
    kept = (
        f"; the GLB holds triangles only, the OBJ and FBX exports keep {paired:,} quads"
        if paired
        else ""
    )
    faces = (
        f"{info['quads_after']:,} quads ({info['triangles_after']:,} triangles in the file{kept})"
        if info["quads_after"] == info["faces_after"]
        else f"{info['faces_after']:,} faces ({info['triangles_after']:,} triangles)"
    )
    detail = f"{info['triangles_before']:,} triangles → {faces}; {distances}."
    if info["colours_transferred"]:
        detail += " Colours were carried over from the nearest point of the original."
    steps = [Step("retopology", title, "applied", detail)]
    baked = [m for m in info["maps"] if m != "base"]
    if "base" in info["maps"]:
        steps.append(
            Step(
                "baking",
                "Texture baking",
                "applied",
                f"The base colour was baked onto the new surface's UVs ({texture_size} px).",
            )
        )
    if settings.maps:
        names = {"normal": "normal map (keeps fine detail)", "occlusion": "ambient occlusion"}
        steps.append(
            Step(
                "maps",
                maps_title,
                "applied" if baked else "skipped",
                f"Baked in Blender ({texture_size} px): " + ", ".join(names[m] for m in baked) + "."
                if baked
                else "Nothing was baked.",
            )
        )
    return result, steps


def _reduce_steps(
    result: ProcessingResult,
    profile: Profile,
    *,
    workdir: Path | None,
    cancel: CancelCheck | None,
    heavy_turn: HeavyTurn | None,
) -> None:
    """Triangle budget, retopology and baking steps (Professional)."""
    if profile.decimate_to is None:
        return
    scene = result.scene
    target = profile.decimate_to
    over = _face_count(scene) > target
    textured = any(_textured(m) and _base_image(m) is not None for _, m in _meshes(scene))
    blender_problem = check_blender() if profile.report_tools else "not used"
    if over and textured and blender_problem is None:
        size = bake_texture_size(scene, profile.max_texture)
        with (heavy_turn or nullcontext)():
            baked, reduce_step, bake_step = bake_reduce(
                scene, target, size, workdir=workdir, cancel=cancel
            )
        if baked is not None:
            result.scene = baked
        result.steps.append(reduce_step)
    else:
        reduce_step = decimate(scene, target)
        result.steps.append(reduce_step)
        if not over:
            detail = (
                f"Not needed: the model is within {target:,} triangles, so its own texture is kept."
            )
        elif reduce_step.status == "applied":
            detail = "Not needed: the model has no texture; it was reduced directly."
        else:
            detail = (
                "Not done: the model has texture coordinates but no texture image to bake from."
            )
        bake_step = Step("baking", "Texture baking", "skipped", detail)
        if over and textured:
            bake_step = Step(
                "baking",
                "Texture baking",
                "unavailable",
                f"{blender_problem} Without it, textured models over {target:,} triangles are "
                "kept as they are.",
            )
    if profile.report_tools:
        result.steps.append(bake_step)


def process(
    scene: trimesh.Scene,
    quality: QualityMode,
    *,
    remove_ground_plane: bool = False,
    scale: ScaleReference | None = None,
    workdir: Path | None = None,
    cancel: CancelCheck | None = None,
    heavy_turn: HeavyTurn | None = None,
    smoothing: int = 0,
    smoothing_method: SmoothingMethod = "sharp",
    remesh_settings: RemeshSettings | None = None,
    upscale: int | None = None,
    upscale_limits: UpscaleLimits | None = None,
) -> ProcessingResult:
    """Run the profile's steps. ``result.scene`` is the processed scene: usually
    ``scene`` changed in place, but a new scene when the model was baked.

    ``heavy_turn`` is entered around Blender (it uses every core for minutes),
    so it waits for other heavy local jobs; the quick steps never wait."""
    profile = PROFILES[quality]
    result = ProcessingResult(scene=scene)
    if remove_ground_plane:
        result.steps.append(remove_ground(scene))
    result.steps.append(cleanup(scene))
    result.steps.append(remove_small_parts(scene, profile.min_part_fraction))
    if profile.fix_normals:
        result.steps.append(fix_normals(scene))
    if profile.fill_holes:
        result.steps.append(fill_holes(scene))
    if smoothing:
        result.steps.append(
            smooth(
                scene,
                smoothing,
                smoothing_method,
                available_bytes=upscale_limits.available_bytes if upscale_limits else None,
            )
        )
    remeshed = False
    if remesh_settings is not None:
        size = bake_texture_size(scene, profile.max_texture)
        with (heavy_turn or nullcontext)():
            rebuilt, steps = remesh(
                scene, remesh_settings, texture_size=size, workdir=workdir, cancel=cancel
            )
        result.steps.extend(steps)
        if rebuilt is not None:
            result.scene = scene = rebuilt
            remeshed = True
    if upscale:
        with (heavy_turn or nullcontext)():
            result.steps.append(
                upscale_textures(
                    scene,
                    upscale,
                    profile.max_texture,
                    workdir=workdir,
                    cancel=cancel,
                    limits=upscale_limits,
                )
            )
    result.steps.append(limit_textures(scene, profile.max_texture))
    if not remeshed:  # a rebuilt surface already has the face count asked for
        _reduce_steps(result, profile, workdir=workdir, cancel=cancel, heavy_turn=heavy_turn)
    if scale is not None:
        step, result.scale = apply_scale(result.scene, scale)
        result.steps.append(step)
    return result
