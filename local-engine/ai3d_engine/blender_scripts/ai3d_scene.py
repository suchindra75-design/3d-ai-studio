"""AI 3D Studio's scene script: builds and renders a CGI shot. Runs *inside* Blender.

Started only by :mod:`ai3d_engine.blender` as::

    blender --background --factory-startup --python-exit-code 1 \
        --python ai3d_scene.py -- render_scene <request.json> <result.json>

The request (written by the app from a validated scene plan: file paths, names
from a fixed set and numbers, never code) lists the objects (GLB files with their
size, place, facing and motion), the set and lighting, the camera move, the look
and the render settings. The script

1. imports each model, scales it to its real size and stands it on the ground,
2. builds the set (a seamless studio backdrop or a large procedural ground) and the
   light (studio lamps, a physical sky with the sun, night, neon, space),
3. rigs the camera (a boom on a turning pivot, so orbits follow an arc) for the
   requested move, framed on everything the objects cover while they move,
4. keyframes the objects' motions,
5. renders a still (the hero frame) and the shot as an H.264 MP4, writing
   progress to a JSON file after each frame, then saves the scene as a .blend file
   (textures packed) and a GLB (objects, lights and the camera, animated).

EEVEE is used for speed; when it cannot render on this machine, Cycles on the CPU
takes over. Blender's axes: Z up, the camera looks along +Y at the start of a
straight-on shot; the models (glTF, +Z front) arrive facing -Y, towards it.
"""

import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import bpy  # type: ignore[import-not-found]
import mathutils  # type: ignore[import-not-found]

COMMANDS = ("render_scene",)
LIGHTING = ("studio", "daylight", "golden_hour", "overcast", "night", "neon", "space")
GROUNDS = ("studio", "grass", "sand", "concrete", "water", "snow", "none")
SHOTS = ("static", "orbit", "push_in", "pull_out", "pan", "crane_up", "fly_by")
ANGLES = {"eye": 8.0, "low": 3.0, "high": 28.0, "top": 62.0}
MOTIONS = ("none", "spin", "bob", "hover", "drive", "sway")
STYLES = ("realistic", "cinematic", "stylised", "miniature")


class RequestError(Exception):
    pass


# -- helpers ----------------------------------------------------------------------------


def _vec(values: Any, size: int = 3) -> mathutils.Vector:
    return mathutils.Vector([float(v) for v in values][:size])


def _rgb(values: Any, default: tuple[float, float, float]) -> tuple[float, float, float, float]:
    if not values:
        return (*default, 1.0)
    r, g, b = (max(0.0, min(1.0, float(v))) for v in values[:3])
    return (r, g, b, 1.0)


def _write_progress(path: Path | None, stage: str, done: int, total: int) -> None:
    if path is None:
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"stage": stage, "done": done, "total": total}), encoding="utf-8")
    tmp.replace(path)


def _world_bounds(objects: list[Any]) -> tuple[mathutils.Vector, mathutils.Vector]:
    low = mathutils.Vector((math.inf, math.inf, math.inf))
    high = -low
    found = False
    for obj in objects:
        if obj.type != "MESH":
            continue
        for corner in obj.bound_box:
            point = obj.matrix_world @ mathutils.Vector(corner)
            low = mathutils.Vector(map(min, low, point))
            high = mathutils.Vector(map(max, high, point))
            found = True
    if not found:
        raise RequestError("A model file holds no mesh.")
    return low, high


def _new_material(name: str) -> tuple[Any, Any, Any]:
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    return material, nodes, links


def _principled(nodes: Any) -> Any:
    return next(n for n in nodes if n.type == "BSDF_PRINCIPLED")


# -- objects ----------------------------------------------------------------------------


def _import_object(spec: dict[str, Any], index: int) -> dict[str, Any]:
    path = Path(spec["path"])
    if not path.is_file() or path.suffix.lower() != ".glb":
        raise RequestError(f"Model file missing: {path.name}")
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(path))
    new = [o for o in bpy.data.objects if o not in before]
    meshes = [o for o in new if o.type == "MESH"]
    bpy.context.view_layer.update()
    low, high = _world_bounds(meshes)
    dims = high - low
    largest = max(dims.x, dims.y, dims.z, 1e-6)
    scale = float(spec["size_m"]) / largest

    holder = bpy.data.objects.new(f"{index:02d} {spec['name']}"[:60], None)
    bpy.context.scene.collection.objects.link(holder)
    holder.empty_display_type = "PLAIN_AXES"
    # Put the model's bottom centre at the holder's origin, then scale the holder.
    offset = mathutils.Vector(((low.x + high.x) / 2, (low.y + high.y) / 2, low.z))
    for obj in new:
        if obj.parent is None:
            obj.parent = holder
            obj.location = obj.location - offset
    holder.scale = (scale, scale, scale)
    x, y = (float(v) for v in spec.get("position", (0.0, 0.0)))
    holder.location = (x, y, float(spec.get("elevation_m", 0.0)))
    holder.rotation_euler = (0.0, 0.0, math.radians(float(spec.get("facing_deg", 0.0))))
    for obj in meshes:
        obj.visible_shadow = True
    # A point at the middle of the object, for the camera's focus.
    focus = bpy.data.objects.new(f"{holder.name} focus", None)
    bpy.context.scene.collection.objects.link(focus)
    focus.parent = holder
    focus.location = (0.0, 0.0, dims.z / 2)
    return {
        "name": spec["name"],
        "holder": holder,
        "meshes": meshes,
        "focus": focus,
        "size": [round(d * scale, 3) for d in dims],
        "motion": spec.get("motion", "none"),
    }


def _key(obj: Any, path: str, frame: int, index: int = -1) -> None:
    obj.keyframe_insert(data_path=path, frame=frame, index=index)


def _linear(obj: Any) -> None:
    action = obj.animation_data.action if obj.animation_data else None
    if action is None:
        return
    curves = getattr(action, "fcurves", None)
    if curves is None:  # Blender 5 layered actions
        curves = [
            c
            for layer in action.layers
            for s in layer.strips
            for b in s.channelbags
            for c in b.fcurves
        ]
    for curve in curves:
        for point in curve.keyframe_points:
            point.interpolation = "LINEAR"


def _spin(holder: Any, frames: int, fps: int) -> None:
    yaw = holder.rotation_euler.z
    _key(holder, "rotation_euler", 1, 2)
    holder.rotation_euler.z = yaw + 2 * math.pi * max(1, round(frames / fps / 6))
    _key(holder, "rotation_euler", frames, 2)
    _linear(holder)
    holder.rotation_euler.z = yaw


def _float(holder: Any, frames: int, fps: int, size: float, *, hover: bool) -> float:
    """Bob (or hover: lifted, with a slow sway of its heading); returns the rise."""
    base_z, yaw = holder.location.z, holder.rotation_euler.z
    lift = 0.25 * size if hover else 0.0
    amplitude = 0.05 * size
    step = 2.4 * fps / 4
    frame, phase = 1.0, 0
    while frame <= frames + step:  # quarter periods: middle, top, middle, bottom
        holder.location.z = base_z + lift + amplitude * (0, 1, 0, -1)[phase % 4]
        _key(holder, "location", round(frame), 2)
        if hover:
            holder.rotation_euler.z = yaw + math.radians(10) * math.sin(phase * math.pi / 4)
            _key(holder, "rotation_euler", round(frame), 2)
        frame += step
        phase += 1
    holder.location.z, holder.rotation_euler.z = base_z, yaw
    return lift + amplitude


def _drive(holder: Any, frames: int, fps: int, size: float) -> mathutils.Vector:
    """Moves forward through its place; returns half the path, for framing."""
    base, yaw = holder.location.copy(), holder.rotation_euler.z
    distance = min(1.6 * size * frames / fps / 4, 12 * size)
    forward = mathutils.Vector((-math.sin(yaw), -math.cos(yaw), 0.0))  # its front is -Y
    holder.location = base - forward * distance / 2
    _key(holder, "location", 1)
    holder.location = base + forward * distance / 2
    _key(holder, "location", frames)
    _linear(holder)
    holder.location = base
    return mathutils.Vector((abs(forward.x), abs(forward.y), 0.0)) * distance / 2


def _sway(holder: Any, frames: int, fps: int) -> None:
    half = 1.5 * fps
    frame, phase = 1.0, 0
    while frame <= frames + half:
        holder.rotation_euler.x = math.radians(2.5) * (1 if phase % 2 == 0 else -1)
        _key(holder, "rotation_euler", round(frame), 0)
        frame += half
        phase += 1
    holder.rotation_euler.x = 0.0


def _animate(item: dict[str, Any], frames: int, fps: int) -> mathutils.Vector:
    """Keyframe the object's motion; returns how far it moves from its place, per
    axis, for framing."""
    holder, motion, size = item["holder"], item["motion"], max(item["size"])
    extent = mathutils.Vector((0.0, 0.0, 0.0))
    if motion == "spin":
        _spin(holder, frames, fps)
    elif motion in {"bob", "hover"}:
        extent.z = _float(holder, frames, fps, size, hover=motion == "hover")
    elif motion == "drive":
        extent = _drive(holder, frames, fps, size)
    elif motion == "sway":
        _sway(holder, frames, fps)
    return extent


# -- set and light ----------------------------------------------------------------------


def _ground_material(kind: str, colour: Any) -> Any:
    material, nodes, links = _new_material(f"Ground ({kind})")
    bsdf = _principled(nodes)
    defaults = {
        "studio": ((0.78, 0.78, 0.8), (0.72, 0.72, 0.74), 0.55, 0.0, 1.0),
        "grass": ((0.08, 0.22, 0.04), (0.18, 0.32, 0.07), 0.9, 0.35, 90.0),
        "sand": ((0.72, 0.6, 0.42), (0.62, 0.5, 0.34), 0.85, 0.25, 40.0),
        "concrete": ((0.38, 0.38, 0.37), (0.27, 0.27, 0.27), 0.75, 0.1, 25.0),
        "water": ((0.02, 0.06, 0.09), (0.03, 0.09, 0.12), 0.04, 0.15, 6.0),
        "snow": ((0.92, 0.94, 0.97), (0.84, 0.87, 0.92), 0.55, 0.12, 30.0),
    }[kind]
    a, b, roughness, bump_strength, scale = defaults
    if colour:
        r, g, bl, _alpha = _rgb(colour, a)
        a, b = (r, g, bl), (r * 0.85, g * 0.85, bl * 0.85)
    bsdf.inputs["Roughness"].default_value = roughness
    if kind == "water":
        bsdf.inputs["Base Color"].default_value = (*a, 1.0)
    else:
        noise = nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = scale
        noise.inputs["Detail"].default_value = 6.0
        ramp = nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].color = (*a, 1.0)
        ramp.color_ramp.elements[1].color = (*b, 1.0)
        links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
        links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    if bump_strength > 0:
        bump_noise = nodes.new("ShaderNodeTexNoise")
        bump_noise.inputs["Scale"].default_value = scale * (4 if kind != "water" else 1)
        bump_noise.inputs["Detail"].default_value = 8.0
        if kind == "water":  # slow waves
            bump_noise.noise_dimensions = "4D"
            driver = bump_noise.inputs["W"].driver_add("default_value").driver
            driver.expression = "frame / 60"
        bump = nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = bump_strength
        links.new(bump_noise.outputs["Fac"], bump.inputs["Height"])
        links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return material


def _cyclorama(radius: float, material: Any) -> Any:
    """A seamless studio backdrop: floor curving up into a back wall."""
    width = radius * 24
    depth = radius * 4
    height = radius * 10
    bend = radius * 1.5
    profile = [(-depth, 0.0), (depth - bend, 0.0)]
    for step in range(1, 13):
        a = step / 12 * math.pi / 2
        profile.append((depth - bend + bend * math.sin(a), bend - bend * math.cos(a)))
    profile.append((depth, height))
    verts: list[tuple[float, float, float]] = []
    for x in (-width / 2, width / 2):
        verts.extend((x, y, z) for y, z in profile)
    n = len(profile)
    faces = [(i, i + 1, n + i + 1, n + i) for i in range(n - 1)]
    mesh = bpy.data.meshes.new("Backdrop")
    mesh.from_pydata(verts, [], faces)
    for polygon in mesh.polygons:
        polygon.use_smooth = True
    obj = bpy.data.objects.new("Backdrop", mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(material)
    return obj


def _ground(kind: str, colour: Any, radius: float, behind_y: float) -> Any | None:
    if kind == "none":
        return None
    material = _ground_material(kind, colour)
    if kind == "studio":
        backdrop = _cyclorama(radius, material)
        backdrop.location.y = behind_y
        return backdrop
    bpy.ops.mesh.primitive_circle_add(vertices=96, radius=400.0, fill_type="NGON")
    ground = bpy.context.active_object
    ground.name = "Ground"
    ground.data.materials.append(material)
    return ground


def _world(lighting: str, sky_colour: Any, key_azimuth: float) -> None:
    world = bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    nodes, links = world.node_tree.nodes, world.node_tree.links
    background = nodes["Background"]
    output = nodes["World Output"]
    if lighting in {"daylight", "golden_hour"}:
        sky = nodes.new("ShaderNodeTexSky")
        sky.sky_type = "NISHITA"
        sky.sun_disc = False
        sky.sun_elevation = math.radians(48 if lighting == "daylight" else 5)
        sky.sun_rotation = key_azimuth + math.pi
        sky.altitude = 100.0
        links.new(sky.outputs["Color"], background.inputs["Color"])
        background.inputs["Strength"].default_value = 0.22 if lighting == "daylight" else 0.35
        return
    base = {
        "studio": (0.42, 0.42, 0.44),
        "overcast": (0.55, 0.58, 0.62),
        "night": (0.006, 0.01, 0.03),
        "neon": (0.004, 0.003, 0.008),
        "space": (0.0, 0.0, 0.0),
    }[lighting]
    colour = _rgb(sky_colour, base)
    strength = {"studio": 0.35, "overcast": 0.9}.get(lighting, 1.0)
    background.inputs["Color"].default_value = colour
    background.inputs["Strength"].default_value = strength
    if lighting in {"night", "space"}:
        # Stars: a small dot near the centre of some Voronoi cells, on the dark sky.
        coords = nodes.new("ShaderNodeTexCoord")
        cells = nodes.new("ShaderNodeTexVoronoi")
        cells.inputs["Scale"].default_value = 180.0
        cells.inputs["Randomness"].default_value = 1.0
        ramp = nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].position = 0.0
        ramp.color_ramp.elements[0].color = (1.0, 1.0, 1.0, 1.0)
        ramp.color_ramp.elements[1].position = 0.045
        ramp.color_ramp.elements[1].color = (0.0, 0.0, 0.0, 1.0)
        stars = nodes.new("ShaderNodeBackground")
        stars.inputs["Strength"].default_value = 3.0 if lighting == "space" else 1.2
        add = nodes.new("ShaderNodeAddShader")
        links.new(coords.outputs["Generated"], cells.inputs["Vector"])
        links.new(cells.outputs["Distance"], ramp.inputs["Fac"])
        links.new(ramp.outputs["Color"], stars.inputs["Color"])
        links.new(background.outputs["Background"], add.inputs[0])
        links.new(stars.outputs["Background"], add.inputs[1])
        links.new(add.outputs["Shader"], output.inputs["Surface"])


def _lamp(
    name: str,
    kind: str,
    energy: float,
    colour: tuple[float, float, float],
    *,
    position: mathutils.Vector | None = None,
    target: mathutils.Vector | None = None,
    direction: tuple[float, float] | None = None,
    size: float = 1.0,
) -> Any:
    data = bpy.data.lights.new(name, kind)
    data.energy = energy
    data.color = colour
    if kind == "AREA":
        data.shape = "DISK"
        data.size = size
    elif kind == "SUN":
        data.angle = math.radians(size)
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    if direction is not None:  # (azimuth, elevation) the light comes from
        azimuth, elevation = direction
        obj.rotation_euler = (math.pi / 2 - elevation, 0.0, azimuth + math.pi)
    elif position is not None and target is not None:
        obj.location = position
        obj.rotation_euler = (target - position).to_track_quat("-Z", "Y").to_euler()
    return obj


def _around(
    centre: mathutils.Vector, distance: float, azimuth: float, elevation: float
) -> mathutils.Vector:
    """A point seen from ``centre``; azimuth 0 is in front (-Y), positive to the right."""
    return centre + distance * mathutils.Vector(
        (
            math.sin(azimuth) * math.cos(elevation),
            -math.cos(azimuth) * math.cos(elevation),
            math.sin(elevation),
        )
    )


def _lights(lighting: str, centre: mathutils.Vector, radius: float, key: float) -> None:
    d = max(radius * 2.5, 1.0)
    power = 40.0 * d * d  # an area lamp at distance d gives about the same light at any scale
    warm, cool = (1.0, 0.92, 0.84), (0.85, 0.9, 1.0)
    if lighting == "studio":
        _lamp(
            "Key",
            "AREA",
            power,
            warm,
            position=_around(centre, d, key - 0.8, 0.7),
            target=centre,
            size=d * 0.8,
        )
        _lamp(
            "Fill",
            "AREA",
            power * 0.3,
            cool,
            position=_around(centre, d, key + 1.1, 0.35),
            target=centre,
            size=d,
        )
        _lamp(
            "Rim",
            "AREA",
            power * 0.6,
            (1.0, 1.0, 1.0),
            position=_around(centre, d, key + math.pi, 0.9),
            target=centre,
            size=d * 0.5,
        )
    elif lighting == "daylight":
        _lamp(
            "Sun", "SUN", 4.0, (1.0, 0.97, 0.92), direction=(key - 0.9, math.radians(48)), size=1.0
        )
    elif lighting == "golden_hour":
        _lamp(
            "Sun", "SUN", 3.2, (1.0, 0.62, 0.34), direction=(key - 1.2, math.radians(8)), size=1.5
        )
    elif lighting == "overcast":
        _lamp(
            "Sun", "SUN", 0.9, (0.95, 0.97, 1.0), direction=(key - 0.6, math.radians(60)), size=25.0
        )
    elif lighting == "night":
        _lamp(
            "Moon", "SUN", 0.25, (0.6, 0.7, 1.0), direction=(key + 0.8, math.radians(35)), size=1.0
        )
        _lamp(
            "Lamp",
            "AREA",
            power * 0.22,
            (1.0, 0.62, 0.32),
            position=_around(centre, d * 0.8, key - 0.7, 0.6),
            target=centre,
            size=d * 0.3,
        )
    elif lighting == "neon":
        _lamp(
            "Magenta",
            "AREA",
            power * 0.45,
            (1.0, 0.1, 0.6),
            position=_around(centre, d, key - 1.3, 0.35),
            target=centre,
            size=d * 0.4,
        )
        _lamp(
            "Cyan",
            "AREA",
            power * 0.45,
            (0.1, 0.75, 1.0),
            position=_around(centre, d, key + 1.3, 0.35),
            target=centre,
            size=d * 0.4,
        )
        _lamp(
            "Top",
            "AREA",
            power * 0.15,
            (1.0, 1.0, 1.0),
            position=_around(centre, d, key + math.pi, 1.2),
            target=centre,
            size=d,
        )
    elif lighting == "space":
        _lamp(
            "Star", "SUN", 5.0, (1.0, 0.98, 0.95), direction=(key - 1.4, math.radians(15)), size=0.5
        )


# -- camera -----------------------------------------------------------------------------


def _distance(
    lens: float, aspect: float, extents: mathutils.Vector, elevation: float
) -> tuple[float, float]:
    """How far the camera stands so the objects' width (seen from any side) and height
    fill the frame with a margin; and the objects' radius."""
    half_h = math.atan(18.0 / lens)
    half_v = math.atan(18.0 / aspect / lens)
    across = math.hypot(extents.x, extents.y)
    tall = extents.z * math.cos(elevation) + across * math.sin(elevation)
    distance = max((across / 2) / math.tan(half_h), (tall / 2) / math.tan(half_v)) + across / 4
    return max(distance * 1.08, 0.1), max(across, extents.z) / 2


def _rig(centre: mathutils.Vector, data: Any) -> tuple[Any, Any]:
    """The pivot (at the target, turning) and the camera riding on it, aimed at it."""
    pivot = bpy.data.objects.new("Camera pivot", None)
    bpy.context.scene.collection.objects.link(pivot)
    pivot.location = centre
    camera = bpy.data.objects.new("Camera", data)
    bpy.context.scene.collection.objects.link(camera)
    camera.parent = pivot
    bpy.context.scene.camera = camera
    aim = bpy.data.objects.new("Camera aim", None)
    bpy.context.scene.collection.objects.link(aim)
    aim.parent = pivot
    track = camera.constraints.new("TRACK_TO")
    track.target = aim
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"
    return pivot, camera


Key = tuple[int, float, float, float, float]  # frame, azimuth, distance, elevation, truck


def _moves(kind: str, frames: int, dist: float, elev: float, radius: float) -> list[Key]:
    """The camera's keyframes for a move; the first gives the start azimuth."""
    front = math.radians(-25.0)  # a three-quarter view
    sweep = math.radians(min(120.0, 20.0 * frames / 24))
    moves: dict[str, list[Key]] = {
        "static": [(1, front, dist, elev, 0.0)],
        "orbit": [(1, -sweep / 2, dist, elev, 0.0), (frames, sweep / 2, dist, elev, 0.0)],
        "push_in": [
            (1, front - 0.08, dist * 1.35, elev, 0.0),
            (frames, front + 0.08, dist * 0.82, elev, 0.0),
        ],
        "pull_out": [
            (1, front + 0.08, dist * 0.8, elev, 0.0),
            (frames, front - 0.08, dist * 1.4, elev, 0.0),
        ],
        "pan": [(1, 0.0, dist, elev, -radius * 0.9), (frames, 0.0, dist, elev, radius * 0.9)],
        "crane_up": [
            (1, front, dist, math.radians(2.0), 0.0),
            (frames, front + 0.1, dist, math.radians(38.0), 0.0),
        ],
        "fly_by": [
            (1, math.radians(-80), dist * 1.1, elev, 0.0),
            (max(2, frames // 2), 0.0, dist * 0.78, elev + 0.05, 0.0),
            (frames, math.radians(80), dist * 1.1, elev, 0.0),
        ],
    }
    if kind not in moves:
        raise RequestError(f"Unknown camera move: {kind}")
    return moves[kind]


def _camera(
    shot: dict[str, Any],
    style: str,
    framing: tuple[mathutils.Vector, mathutils.Vector, float],
    frames: int,
    focus: Any | None,
) -> tuple[Any, float]:
    """A camera on a boom: the pivot sits at the target and turns (orbits follow an arc);
    the camera rides at (0, -distance, height) on it. ``framing``: the target, the
    extents to keep in view and the frame's aspect. Returns the camera and the start
    azimuth (radians), which the lights are placed from."""
    centre, extents, aspect = framing
    lens = max(14.0, min(200.0, float(shot.get("lens_mm", 35.0))))
    kind = shot.get("shot", "orbit")
    elevation = math.radians(ANGLES.get(shot.get("angle", "eye"), 8.0))
    if style == "miniature":
        elevation = max(elevation, math.radians(35))
    distance, radius = _distance(lens, aspect, extents, elevation)

    data = bpy.data.cameras.new("Camera")
    data.lens = lens
    data.sensor_width = 36.0
    data.clip_start = max(0.01, radius / 200)
    data.clip_end = max(1000.0, radius * 400)
    pivot, camera = _rig(centre, data)
    keys = _moves(kind, frames, distance, elevation, radius)
    for frame, azimuth, dist, elev, truck in keys:
        pivot.rotation_euler = (0.0, 0.0, azimuth)
        pivot.location = centre + mathutils.Vector(
            (truck * math.cos(azimuth), truck * math.sin(azimuth), 0.0)
        )
        camera.location = (0.0, -dist * math.cos(elev), dist * math.sin(elev))
        _key(pivot, "rotation_euler", frame, 2)
        _key(pivot, "location", frame)
        _key(camera, "location", frame)
    if kind == "orbit":
        _linear(pivot)

    # Depth of field: a little for realism, more for a cinematic look, a lot for a
    # miniature (tilt-shift) look; none for a stylised one.
    stops = {"realistic": 8.0, "cinematic": 2.8, "miniature": 1.2}.get(style)
    if stops is not None and focus is not None:
        data.dof.use_dof = True
        data.dof.focus_object = focus
        data.dof.aperture_fstop = stops
    return camera, keys[0][1]


# -- render -----------------------------------------------------------------------------


def _set_engine(scene: Any, engine: str, samples: int, *, raytracing: bool = False) -> str:
    if engine == "cycles":
        scene.render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = samples
        scene.cycles.use_denoising = True
        scene.cycles.use_adaptive_sampling = True
        return "cycles"
    for name in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        try:
            scene.render.engine = name
            break
        except TypeError:
            continue
    scene.eevee.taa_render_samples = samples
    if hasattr(scene.eevee, "use_raytracing"):
        scene.eevee.use_raytracing = raytracing
    if hasattr(scene.eevee, "use_shadows"):
        scene.eevee.use_shadows = True
    return "eevee"


def _look(scene: Any, style: str, lighting: str) -> None:
    view = scene.view_settings
    try:
        view.view_transform = "AgX"
        view.look = {
            "cinematic": "AgX - Medium High Contrast",
            "stylised": "AgX - Punchy",
            "miniature": "AgX - Punchy",
        }.get(style, "AgX - Base Contrast")
    except TypeError:
        pass
    view.exposure = {"night": 0.3, "neon": 0.2, "overcast": 0.2}.get(lighting, 0.0)


def _render(scene: Any, settings: dict[str, Any], still: bool) -> str:
    """Render (a still or the animation) with the requested engine; Cycles on the CPU
    when EEVEE fails on this machine. Returns the engine used."""
    engine = settings["still_engine" if still else "engine"]
    samples = int(settings["still_samples" if still else "samples"])
    used = _set_engine(scene, engine, samples, raytracing=bool(settings.get("raytracing")))
    try:
        bpy.ops.render.render(animation=not still, write_still=still)
    except RuntimeError:
        if used == "cycles":
            raise
        used = _set_engine(scene, "cycles", max(16, samples // 2))
        bpy.ops.render.render(animation=not still, write_still=still)
    return used


def _checked(request: dict[str, Any]) -> None:
    environment = request.get("environment", {})
    if (
        request.get("style", "realistic") not in STYLES
        or environment.get("lighting", "studio") not in LIGHTING
        or environment.get("ground", "studio") not in GROUNDS
    ):
        raise RequestError("Unknown style, lighting or ground.")
    if request.get("camera", {}).get("shot", "orbit") not in SHOTS:
        raise RequestError("Unknown camera move.")
    if not request.get("objects"):
        raise RequestError("The scene has no objects.")
    for spec in request["objects"]:
        if spec.get("motion", "none") not in MOTIONS:
            raise RequestError(f"Unknown motion for {spec.get('name')}.")


def _covered(items: list[dict[str, Any]], frames: int, fps: int) -> tuple[Any, Any]:
    """Animate the objects; the box the camera must keep in view: every object,
    wherever its motion takes it."""
    low = mathutils.Vector((math.inf, math.inf, math.inf))
    high = -low
    for item in items:
        extent = _animate(item, frames, fps)
        a, b = _world_bounds(item["meshes"])
        a, b = a - extent, b + extent
        if item["motion"] in {"spin", "hover"}:  # it turns: any side may face out
            middle = (a + b) / 2
            half = max(b.x - a.x, b.y - a.y) / 2
            a = mathutils.Vector((middle.x - half, middle.y - half, a.z))
            b = mathutils.Vector((middle.x + half, middle.y + half, b.z))
        low = mathutils.Vector(map(min, low, a))
        high = mathutils.Vector(map(max, high, b))
    return low, high


def _build(request: dict[str, Any], frames: int, aspect: float) -> tuple[Any, dict[str, Any]]:
    """Objects, motions, camera, sky, lights, ground and look. Returns the camera and
    what the result reports about them."""
    style = request.get("style", "realistic")
    environment = request.get("environment", {})
    lighting = environment.get("lighting", "studio")
    shot = request.get("camera", {})
    items = [_import_object(spec, n) for n, spec in enumerate(request["objects"])]
    bpy.context.view_layer.update()
    low, high = _covered(items, frames, bpy.context.scene.render.fps)
    centre = (low + high) / 2
    centre.z = low.z + (high.z - low.z) * (0.45 if shot.get("angle") != "low" else 0.6)
    radius = max((high - low).length / 2, 0.05)

    largest = max(items, key=lambda i: max(i["size"]))
    focus = next((i for i in items if i["name"] == shot.get("focus")), largest)
    camera, start_azimuth = _camera(
        shot, style, (centre, high - low, aspect), frames, focus["focus"]
    )
    _world(lighting, environment.get("sky_colour"), start_azimuth)
    _lights(lighting, centre, radius, start_azimuth)
    _ground(
        environment.get("ground", "studio"),
        environment.get("ground_colour"),
        radius,
        high.y + radius * 0.6,
    )
    _look(bpy.context.scene, style, lighting)
    return camera, {
        "objects": [{"name": i["name"], "size_m": i["size"]} for i in items],
        "camera": {
            "shot": shot.get("shot", "orbit"),
            "lens_mm": camera.data.lens,
            "focus": focus["name"],
        },
        "radius_m": round(radius, 3),
    }


def _still(scene: Any, render: dict[str, Any], path: str, frames: int) -> str:
    """The hero still: where the move is 60% done."""
    scene.frame_set(max(1, round(frames * 0.6)))
    scene.render.resolution_x = int(render.get("still_width", render["width"]))
    scene.render.resolution_y = int(render.get("still_height", render["height"]))
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.filepath = path
    return _render(scene, render, still=True)


def _video(
    scene: Any, render: dict[str, Any], path: str, frames: int, progress: Path | None
) -> str:
    """The shot as H.264 in MP4, reporting progress after each frame."""
    scene.render.resolution_x, scene.render.resolution_y = (
        int(render["width"]),
        int(render["height"]),
    )
    scene.frame_set(1)
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.ffmpeg.constant_rate_factor = "HIGH"
    scene.render.ffmpeg.ffmpeg_preset = "GOOD"
    scene.render.ffmpeg.audio_codec = "NONE"
    scene.render.filepath = path
    scene.render.use_file_extension = False
    done = [0]

    def frame_written(_scene: Any, *_: Any) -> None:
        done[0] += 1
        _write_progress(progress, "video", done[0], frames)

    bpy.app.handlers.render_write.append(frame_written)
    try:
        engine = _render(scene, render, still=False)
    finally:
        bpy.app.handlers.render_write.remove(frame_written)
    if not Path(path).is_file():
        raise RequestError("Blender wrote no video file.")
    return engine


def _save(camera: Any, output: dict[str, Any], frames: int) -> None:
    """The .blend file (textures packed) and an animated GLB."""
    bpy.context.scene.frame_set(1)
    if output.get("blend"):
        bpy.ops.file.pack_all()
        bpy.ops.wm.save_as_mainfile(filepath=str(output["blend"]), compress=True, copy=True)
    if output.get("glb"):
        # Bake the camera rig into plain keyframes so other programs get the move.
        bpy.ops.object.select_all(action="DESELECT")
        camera.select_set(True)
        bpy.context.view_layer.objects.active = camera
        bpy.ops.nla.bake(
            frame_start=1,
            frame_end=frames,
            only_selected=True,
            visual_keying=True,
            clear_constraints=True,
            clear_parents=True,
            bake_types={"OBJECT"},
        )
        bpy.ops.export_scene.gltf(
            filepath=str(output["glb"]),
            export_format="GLB",
            export_cameras=True,
            export_lights=True,
            export_animations=True,
            export_yup=True,
        )


def render_scene(request: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    _checked(request)
    output, render = request["output"], request["render"]
    progress = Path(output["progress"]) if output.get("progress") else None

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    fps = int(render["fps"])
    frames = max(1, round(float(render["seconds"]) * fps))
    scene.render.fps = fps
    scene.frame_start, scene.frame_end = 1, frames
    scene.render.resolution_percentage = 100
    scene.render.use_motion_blur = bool(render.get("motion_blur"))
    scene.render.film_transparent = False

    _write_progress(progress, "building", 0, frames)
    camera, facts = _build(request, frames, int(render["width"]) / int(render["height"]))
    timings = {"build": time.monotonic() - started}
    engines: dict[str, str] = {}
    _write_progress(progress, "still", 0, frames)
    t = time.monotonic()
    engines["still"] = _still(scene, render, str(output["still"]), frames)
    timings["still"] = time.monotonic() - t
    t = time.monotonic()
    engines["video"] = _video(scene, render, str(output["video"]), frames, progress)
    timings["video"] = time.monotonic() - t
    _write_progress(progress, "saving", frames, frames)
    t = time.monotonic()
    _save(camera, output, frames)
    timings["saving"] = time.monotonic() - t
    return {
        "frames": frames,
        "fps": fps,
        "width": int(render["width"]),
        "height": int(render["height"]),
        "engines": engines,
        **facts,
        "seconds": {k: round(v, 2) for k, v in timings.items()}
        | {"total": round(time.monotonic() - started, 2)},
    }


def main(argv: list[str]) -> int:
    args = argv[argv.index("--") + 1 :] if "--" in argv else []
    if len(args) != 3 or args[0] not in COMMANDS:
        print("usage: ... -- render_scene <request.json> <result.json>")
        return 2
    request_path, result_path = Path(args[1]), Path(args[2])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    try:
        result: dict[str, Any] = {"ok": True, **render_scene(request)}
    except RequestError as exc:
        result = {"ok": False, "error": str(exc)}
    except RuntimeError as exc:  # Blender operators raise RuntimeError on failure
        result = {"ok": False, "error": str(exc).strip().splitlines()[-1][:500]}
    result["blender"] = bpy.app.version_string
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
