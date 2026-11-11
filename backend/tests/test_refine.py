"""Refining locally generated models (processing/refine.py) on shapes with known answers.

Spheres stand in for TripoSR's output: their colours, outline and hidden side are
known exactly, so the cleanup, simplifying, unwrapping, baking and photo projection
can each be checked against the right answer.
"""

import cv2
import numpy as np
import pytest
import trimesh

from ai3d.processing import refine as rf

RED, BLUE = (255, 0, 0), (0, 0, 255)


def _sphere(
    *,
    subdivisions: int = 5,
    radius: float = 0.4,
    noise: float = 0.0,
    colour: tuple[int, ...] | None = None,
) -> trimesh.Trimesh:
    """An icosphere with linear vertex colours: red on the left (x < 0), blue on the
    right, or one colour."""
    sphere = trimesh.creation.icosphere(subdivisions=subdivisions, radius=radius)
    if noise:
        rng = np.random.default_rng(3)
        scale = 1 + rng.normal(0, noise, len(sphere.vertices))
        sphere.vertices = sphere.vertices * scale[:, None]
    if colour is None:
        rgb = np.where(sphere.vertices[:, :1] < 0, RED, BLUE)
    else:
        rgb = np.tile(colour, (len(sphere.vertices), 1))
    sphere.visual.vertex_colors = np.hstack([rgb, np.full((len(rgb), 1), 255)]).astype(np.uint8)
    return sphere


def _texture_colours(mesh: trimesh.Trimesh, points: np.ndarray) -> np.ndarray:
    """The texture's sRGB colour (0..255) at the mesh vertex nearest each point."""
    image = np.asarray(mesh.visual.material.baseColorTexture.convert("RGB"))
    nearest = mesh.kdtree.query(points)[1]
    uv = mesh.visual.uv[nearest]
    height, width = image.shape[:2]
    x = np.clip((uv[:, 0] * width).astype(int), 0, width - 1)
    y = np.clip(((1 - uv[:, 1]) * height).astype(int), 0, height - 1)
    return image[y, x].astype(float)


def _photo(
    size: int = 512,
    *,
    shift: float = 0.0,
    scale: float = 1.0,
    colours: tuple[tuple[int, ...], ...] = ((0, 200, 0), (200, 0, 200)),
) -> np.ndarray:
    """A square RGBA picture whose cut-out is the sphere's outline from TripoSR's
    camera (optionally moved and scaled), left half one colour, right half another."""
    outline = trimesh.creation.icosphere(subdivisions=5, radius=0.4)
    pixels = rf._pinhole(np.asarray(outline.vertices), size)
    centre = size / 2
    pixels = (pixels - centre) * scale + centre + [shift, 0]
    alpha = np.zeros((size, size), np.uint8)
    for triangle in pixels[outline.faces]:
        cv2.fillPoly(alpha, [np.round(triangle).astype(np.int32)], 255)
    rgb = np.zeros((size, size, 3), np.uint8)
    rgb[:, : size // 2] = colours[0]
    rgb[:, size // 2 :] = colours[1]
    return np.dstack([rgb, alpha])


def test_loose_fragments_go_and_real_parts_stay() -> None:
    body = trimesh.creation.icosphere(subdivisions=3, radius=0.4)
    second = trimesh.creation.icosphere(subdivisions=3, radius=0.25)
    second.apply_translation([0.9, 0, 0])
    specks = []
    for k in range(3):
        speck = trimesh.creation.icosphere(subdivisions=1, radius=0.01)
        speck.apply_translation([-0.7, 0.1 * k, 0])
        specks.append(speck)
    mesh = trimesh.util.concatenate([body, second, *specks])
    cleaned, removed, share = rf.remove_fragments(mesh)
    assert removed == 3
    assert 0 < share < 0.002
    assert len(cleaned.split(only_watertight=False)) == 2  # both real parts kept
    assert len(cleaned.faces) == len(body.faces) + len(second.faces)


def test_smoothing_takes_out_noise_without_shrinking() -> None:
    noisy = _sphere(subdivisions=4, noise=0.01)
    smoothed = rf.smooth(noisy, rf.SMOOTH_ITERATIONS)
    before = np.linalg.norm(noisy.vertices, axis=1)
    after = np.linalg.norm(smoothed.vertices, axis=1)
    assert after.std() < before.std() / 2
    assert abs(after.mean() / before.mean() - 1) < 0.01  # Taubin: no shrinking
    assert np.array_equal(smoothed.visual.vertex_colors, noisy.visual.vertex_colors)


def test_refining_simplifies_unwraps_and_bakes_the_colours() -> None:
    sphere = _sphere()  # 20,480 triangles
    refined, report = rf.refine(sphere, rf.RefineSettings(triangles=4000, texture_size=512))
    assert report.triangles_before == 20_480
    assert 3600 <= report.triangles_after <= 4200
    assert report.photo_projected is False
    assert isinstance(refined.visual, trimesh.visual.TextureVisuals)
    assert refined.visual.material.baseColorTexture.size == (512, 512)
    uv = refined.visual.uv
    assert uv.min() >= 0
    assert uv.max() <= 1
    # Charts do not overlap: drawn at a finer size, the covered area equals the sum
    # of the triangles' areas.
    size = 2048
    triangles = np.stack([uv[:, 0], 1 - uv[:, 1]], axis=1)[refined.faces] * size
    covered = np.count_nonzero(rf._face_ids(triangles, size) >= 0)
    edges = triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    total = 0.5 * np.abs(edges[0][:, 0] * edges[1][:, 1] - edges[0][:, 1] * edges[1][:, 0]).sum()
    assert covered / total == pytest.approx(1.0, abs=0.06)
    # Same shape: every refined vertex lies on the sphere.
    assert np.abs(np.linalg.norm(refined.vertices, axis=1) - 0.4).max() < 0.01
    # Colours: red on the left, blue on the right (away from the boundary).
    left = refined.vertices[refined.vertices[:, 0] < -0.1]
    right = refined.vertices[refined.vertices[:, 0] > 0.1]
    assert np.abs(_texture_colours(refined, left) - RED).max() < 12
    assert np.abs(_texture_colours(refined, right) - BLUE).max() < 12
    # Shading is smooth across chart seams: split vertices share one normal.
    normals = refined.vertex_normals
    radial = refined.vertices / np.linalg.norm(refined.vertices, axis=1, keepdims=True)
    assert np.einsum("ij,ij->i", normals, radial).min() > 0.98


def test_the_photo_is_projected_onto_the_front_only() -> None:
    grey = rf.linear_to_srgb(np.array([0.2])) * 255
    sphere = _sphere(colour=(51, 51, 51))  # linear 0.2
    refined, report = rf.refine(
        sphere, rf.RefineSettings(triangles=6000, texture_size=1024), _photo()
    )
    assert report.photo_projected is True
    assert report.photo_fit_iou is not None
    assert report.photo_fit_iou > 0.95
    # Full strength within about 60° of the camera: at most a quarter of a sphere's
    # surface (a cap with 1 - cos 60° = half the height), less with perspective.
    assert 0.12 < report.photo_share < 0.25
    v = refined.vertices
    facing = v[:, 2] > 0.33  # within about 35° of the camera
    front_left = v[facing & (v[:, 0] < -0.05)]
    front_right = v[facing & (v[:, 0] > 0.05)]
    back = v[v[:, 2] < -0.2]
    assert np.abs(_texture_colours(refined, front_left) - (0, 200, 0)).max() < 25
    assert np.abs(_texture_colours(refined, front_right) - (200, 0, 200)).max() < 25
    assert np.abs(_texture_colours(refined, back) - grey).max() < 6


def test_the_camera_fit_finds_a_moved_and_scaled_outline() -> None:
    sphere = trimesh.creation.icosphere(subdivisions=4, radius=0.4)
    picture = _photo(shift=24, scale=1.1)
    fit = rf.fit_camera(sphere, picture[..., 3])
    assert fit.iou > 0.95
    assert fit.scale == pytest.approx(1.1, abs=0.02)
    assert fit.offset[0] == pytest.approx(24 * rf.FIT_SIZE / 512, abs=1.0)
    assert fit.offset[1] == pytest.approx(0, abs=1.0)


def test_a_cutout_that_does_not_match_is_not_projected() -> None:
    sphere = _sphere()
    photo = np.zeros((512, 512, 4), np.uint8)
    photo[10:60, 10:60] = (0, 255, 0, 255)  # a small square in the corner
    refined, report = rf.refine(sphere, rf.RefineSettings(triangles=3000, texture_size=256), photo)
    assert report.photo_projected is False
    assert report.note is not None
    assert "did not line up" in report.note
    left = refined.vertices[refined.vertices[:, 0] < -0.1]
    assert np.abs(_texture_colours(refined, left) - RED).max() < 12  # its own colours


def test_hidden_parts_do_not_get_the_photo() -> None:
    """A small sphere straight behind a big one: the camera cannot see it."""
    front = _sphere(subdivisions=4, radius=0.3, colour=(51, 51, 51))
    front.apply_translation([0, 0, 0.25])
    hidden = _sphere(subdivisions=4, radius=0.12, colour=(51, 51, 51))
    hidden.apply_translation([0, 0, -0.45])
    mesh = trimesh.util.concatenate([front, hidden])
    alpha = np.zeros((512, 512), np.uint8)
    pixels = rf._pinhole(np.asarray(mesh.vertices), 512)
    for triangle in pixels[mesh.faces]:
        cv2.fillPoly(alpha, [np.round(triangle).astype(np.int32)], 255)
    photo = np.dstack([np.full((512, 512, 3), (0, 200, 0), np.uint8), alpha])
    refined, report = rf.refine(mesh, rf.RefineSettings(triangles=6000, texture_size=1024), photo)
    assert report.photo_projected is True
    grey = rf.linear_to_srgb(np.array([0.2])) * 255
    v = refined.vertices
    hidden_front = v[(v[:, 2] < -0.33) & (v[:, 2] > -0.4) & (np.abs(v[:, 0]) < 0.05)]
    assert len(hidden_front) > 0
    assert np.abs(_texture_colours(refined, hidden_front) - grey).max() < 6
    seen = v[v[:, 2] > 0.5]
    assert np.abs(_texture_colours(refined, seen) - (0, 200, 0)).max() < 25


def _closed(vertices: np.ndarray, faces: np.ndarray) -> trimesh.Trimesh:
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.remove_unreferenced_vertices()
    return mesh


def test_a_folded_fin_left_by_simplifying_is_removed() -> None:
    sphere = trimesh.creation.icosphere(subdivisions=2, radius=0.4)
    a, b = sphere.faces[0][:2]
    tip = len(sphere.vertices)
    vertices = np.vstack([sphere.vertices, sphere.vertices[[a, b]].mean(axis=0) * 1.3])
    fin = np.array([[a, b, tip], [b, a, tip]])  # two back-to-back triangles on an edge
    faces = np.vstack([sphere.faces, fin])
    points, mended, _normals, found = rf.close_up(
        vertices, faces, sphere.vertex_normals[[*range(tip), 0]]
    )
    assert found == 1  # the shared edge was used by four triangles
    mesh = _closed(points, mended)
    assert mesh.is_watertight
    assert mesh.is_winding_consistent
    assert len(mended) == len(sphere.faces)


def test_two_shapes_touching_along_an_edge_are_mended_into_a_closed_surface() -> None:
    first = trimesh.creation.box(bounds=[[0, 0, 0], [1, 1, 1]])
    second = trimesh.creation.box(bounds=[[1, 1, 0], [2, 2, 1]])
    joined = trimesh.util.concatenate([first, second])
    joined.merge_vertices()  # the two boxes now share the edge x = y = 1
    assert not joined.is_watertight
    normals = np.asarray(joined.vertex_normals)
    points, faces, mended_normals, found = rf.close_up(
        np.asarray(joined.vertices), np.asarray(joined.faces), normals
    )
    assert found >= 1
    mesh = _closed(points, faces)
    _, counts = rf._edge_counts(np.asarray(mesh.faces))
    assert counts.max() == 2
    assert counts.min() == 2
    assert mesh.is_winding_consistent
    assert mesh.volume == pytest.approx(2.0, rel=0.1)  # the boxes keep their volume
    assert len(mended_normals) == len(points)


def test_refined_models_stay_closed() -> None:
    """Thin spikes that simplifying folds flat: the result is still closed."""
    mesh = trimesh.creation.icosphere(subdivisions=5, radius=0.35)
    tips = np.random.default_rng(5).choice(len(mesh.vertices), 40, replace=False)
    mesh.vertices[tips] *= 1.8  # each tip pulls a needle out of the surface
    mesh.visual.vertex_colors = np.tile([120, 120, 120, 255], (len(mesh.vertices), 1))
    assert mesh.is_watertight
    refined, _report = rf.refine(mesh, rf.RefineSettings(triangles=800, texture_size=256))
    welded = refined.copy()
    welded.merge_vertices(merge_tex=True, merge_norm=True)
    _, counts = rf._edge_counts(np.asarray(welded.faces))
    assert counts.max() == 2
    assert counts.min() == 2
    assert welded.is_winding_consistent
    assert welded.volume > 0


# -- several views ------------------------------------------------------------------


def _ellipsoid(radii: tuple[float, float, float], colour: tuple[int, ...]) -> trimesh.Trimesh:
    shape = trimesh.creation.icosphere(subdivisions=5, radius=1.0)
    shape.vertices = shape.vertices * np.array(radii)
    rgb = np.tile(colour, (len(shape.vertices), 1))
    shape.visual.vertex_colors = np.hstack([rgb, np.full((len(rgb), 1), 255)]).astype(np.uint8)
    return shape


def _view_picture(
    shape: trimesh.Trimesh, role: str | None, colour: tuple[int, ...], size: int = 512
) -> np.ndarray:
    """The shape's outline as seen from a view's camera, filled with one colour."""
    vertices = np.asarray(shape.vertices)
    if role is not None:
        vertices = vertices @ rf.turn_towards(np.array(rf.VIEW_DIRECTIONS[role])).T
    pixels = rf._pinhole(vertices, size)
    alpha = np.zeros((size, size), np.uint8)
    for triangle in pixels[shape.faces]:
        cv2.fillPoly(alpha, [np.round(triangle).astype(np.int32)], 255)
    return np.dstack([np.full((size, size, 3), colour, np.uint8), alpha])


GREY, GREEN, BLUE_SRGB, MAGENTA = (51, 51, 51), (0, 200, 0), (0, 0, 220), (200, 0, 200)


def test_side_photos_correct_a_model_that_is_too_flat() -> None:
    """TripoSR guessed the object 0.2 deep; its side photo shows it is 0.5 deep."""
    real = _ellipsoid((0.25, 0.4, 0.25), GREY)
    model = _ellipsoid((0.25, 0.4, 0.1), GREY)
    front = _view_picture(real, None, GREEN)
    side = 1000.0  # both photos framed alike (same camera, same distance)
    views = [
        rf.ViewPhoto("left", _view_picture(real, "left", BLUE_SRGB), side),
        rf.ViewPhoto("back", _view_picture(real, "back", MAGENTA), side),
    ]
    refined, report = rf.refine(
        model,
        rf.RefineSettings(triangles=6000, texture_size=1024),
        front,
        views=views,
        photo_crop_side=side,
    )
    assert report.depth is not None
    assert report.depth["applied"] is True
    (left,) = report.depth["views"]
    assert left["used"] is True
    assert left["flipped"] is False
    assert left["match"] > 0.9
    depth = float(np.ptp(refined.vertices[:, 2]))
    assert depth == pytest.approx(0.5, rel=0.12)  # was 0.2
    assert float(np.ptp(refined.vertices[:, 0])) == pytest.approx(0.5, rel=0.05)  # unchanged
    # Each side shows its own photo; the front keeps the front photo (the corrected
    # surface is not a perfect ellipsoid, so a few points tilt and mix in some side).
    v = refined.vertices
    front_dev = np.abs(_texture_colours(refined, v[v[:, 2] > 0.2]) - GREEN).max(axis=1)
    assert (front_dev < 30).mean() > 0.85
    assert np.abs(_texture_colours(refined, v[v[:, 0] > 0.22]).mean(axis=0) - BLUE_SRGB).max() < 40
    assert np.abs(_texture_colours(refined, v[v[:, 2] < -0.2]).mean(axis=0) - MAGENTA).max() < 40
    assert {entry["role"]: entry["used"] for entry in report.views} == {"left": True, "back": True}


def test_a_side_photo_that_does_not_fit_leaves_the_depth_alone() -> None:
    model = _ellipsoid((0.25, 0.4, 0.1), GREY)
    front = _view_picture(model, None, GREEN)
    # A shape widest at the top and narrowing to a point: nothing like the model's
    # side, which widens from the top down to the middle.
    stripe = np.zeros((512, 512, 4), np.uint8)
    cv2.fillPoly(stripe, [np.array([[100, 60], [412, 60], [256, 460]], np.int32)], (0, 0, 220, 255))
    _, report = rf.refine(
        model,
        rf.RefineSettings(triangles=4000, texture_size=512),
        front,
        views=[rf.ViewPhoto("left", stripe, 1000.0)],
        photo_crop_side=1000.0,
    )
    assert report.depth is not None
    assert report.depth["applied"] is False
    assert "did not match" in report.depth["views"][0]["note"]


def test_views_are_turned_to_face_the_front_camera() -> None:
    for role, direction in rf.VIEW_DIRECTIONS.items():
        turned = rf.turn_towards(np.array(direction)) @ np.array(direction)
        assert turned == pytest.approx([0.0, 0.0, 1.0], abs=1e-9), role


@pytest.mark.parametrize("made_at", [0.0, 90.0, 200.0])
def test_a_model_made_in_its_own_pose_is_turned_to_face_the_photo(made_at: float) -> None:
    """A figure with a nose and a coloured face, made turned away (as TRELLIS makes
    models); the front photo shows its face. Front and back outlines are mirror
    images, so the colours decide between them."""
    body = _ellipsoid((0.25, 0.4, 0.18), GREY)
    nose = _ellipsoid((0.04, 0.04, 0.08), GREY)
    nose.apply_translation([0.08, 0.15, 0.17])  # off-centre: the outline is not symmetric
    figure = trimesh.util.concatenate([body, nose])
    colours = np.tile([51, 51, 51, 255], (len(figure.vertices), 1))
    colours[figure.vertices[:, 2] > 0.05] = (0, 120, 0, 255)  # the face is green (linear)
    figure.visual.vertex_colors = colours.astype(np.uint8)
    photo = _view_picture(figure, None, (0, 180, 0))  # green where the camera sees
    turned = figure.copy()
    turned.vertices = np.asarray(figure.vertices) @ rf.turn_about_up(made_at).T
    fixed, info = rf.orient_to_photo(turned, photo)
    undo = (360.0 - made_at) % 360.0
    assert min(abs(info["yaw_degrees"] - undo), 360 - abs(info["yaw_degrees"] - undo)) <= 5
    assert np.abs(np.asarray(fixed.vertices) - np.asarray(figure.vertices)).max() < 0.05
