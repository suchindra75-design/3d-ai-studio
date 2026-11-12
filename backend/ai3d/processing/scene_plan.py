"""Scene plans: what a prompt becomes before anything is generated.

A language model (on the user's GPU server) reads the prompt with
:data:`PLANNER_INSTRUCTIONS` and answers with a JSON plan: the objects to make
(each one physical thing with a real size), where they stand, how they move, and
the set, light and camera. Models make mistakes, so :func:`parse_plan` is lenient:
it finds the JSON in the reply, maps near-miss words to the values the renderer
knows ("sunset" → golden hour, "dolly in" → push in), clamps numbers to sensible
ranges and fills gaps with defaults. Only a reply with no usable object at all is
refused.

The renderer (``ai3d_engine/blender_scripts/ai3d_scene.py``) accepts exactly the
values listed here. Once the objects exist, :func:`arrange` places them on the
ground from their real footprints, keeping planned positions where they do not
collide.
"""

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field

from ai3d.models.enums import QualityMode

Lighting = Literal["studio", "daylight", "golden_hour", "overcast", "night", "neon", "space"]
Ground = Literal["studio", "grass", "sand", "concrete", "water", "snow", "none"]
Shot = Literal["static", "orbit", "push_in", "pull_out", "pan", "crane_up", "fly_by"]
Angle = Literal["eye", "low", "high", "top"]
Motion = Literal["none", "spin", "bob", "hover", "drive", "sway"]
Style = Literal["realistic", "cinematic", "stylised", "miniature"]

LIGHTINGS: tuple[str, ...] = get_args(Lighting)
GROUNDS: tuple[str, ...] = get_args(Ground)
SHOTS: tuple[str, ...] = get_args(Shot)
ANGLES: tuple[str, ...] = get_args(Angle)
MOTIONS: tuple[str, ...] = get_args(Motion)
STYLES: tuple[str, ...] = get_args(Style)

MAX_OBJECTS = 6
MIN_SIZE_M, MAX_SIZE_M = 0.03, 60.0
MAX_DISTANCE_M = 200.0
MIN_SECONDS, MAX_SECONDS, DEFAULT_SECONDS = 2.0, 15.0, 6.0


class PlanError(ValueError):
    """The reply holds no usable plan."""


class PlannedObject(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    # What it looks like, for its reference picture.
    description: str = Field(min_length=1, max_length=400)
    # Its largest dimension in metres.
    size_m: float = Field(ge=MIN_SIZE_M, le=MAX_SIZE_M)
    # On the ground: x to the right, y away from the camera, metres; None = let the
    # layout choose.
    position_m: tuple[float, float] | None = None
    elevation_m: float = Field(default=0.0, ge=0.0, le=MAX_DISTANCE_M)
    # Turn about the vertical; 0 faces the camera, positive turns it to its left.
    facing_deg: float = Field(default=0.0, ge=-180.0, le=180.0)
    motion: Motion = "none"
    # Set once the object's 3D model exists.
    model_id: str | None = None
    model_version_id: str | None = None


class ScenePlan(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    style: Style = "cinematic"
    lighting: Lighting = "studio"
    ground: Ground = "studio"
    ground_colour: tuple[float, float, float] | None = None  # linear 0..1
    sky_colour: tuple[float, float, float] | None = None
    shot: Shot = "orbit"
    angle: Angle = "eye"
    lens_mm: float = Field(default=35.0, ge=14.0, le=200.0)
    focus: str | None = None  # the name of the object to keep sharp
    objects: list[PlannedObject] = Field(min_length=1, max_length=MAX_OBJECTS)


PLANNER_INSTRUCTIONS = f"""You plan short CGI shots for a 3D studio app. Read the user's \
scene description and answer with ONE JSON object only (no prose, no code fence) in \
exactly this shape:

{{"title": "short title",
 "style": one of {list(STYLES)},
 "lighting": one of {list(LIGHTINGS)},
 "ground": one of {list(GROUNDS)},
 "ground_colour": "#rrggbb" or null,
 "sky_colour": "#rrggbb" or null,
 "shot": one of {list(SHOTS)},
 "angle": one of {list(ANGLES)},
 "lens_mm": number from 14 to 200 (35 is natural, 85 is a close portrait look),
 "focus": the name of the main object,
 "objects": [
   {{"name": "short name",
    "description": "what it looks like: shape, materials, colours, details",
    "size_m": its largest dimension in metres (a real, believable size),
    "position_m": [x, y] in metres on the ground (x to the right, y away from the camera, \
the main object near [0, 0]),
    "elevation_m": metres above the ground (0 if it stands on it),
    "facing_deg": degrees it is turned (0 faces the camera, 90 faces left, -90 faces right),
    "motion": one of {list(MOTIONS)}}}
 ]}}

Rules:
- 1 to {MAX_OBJECTS} objects. Each object is ONE physical thing that can be modelled on \
its own (a car, a tree, a robot, a lamp). Never make the ground, sky, water, fog, a room, \
a landscape or light an object: use "ground" and "lighting" for those.
- Use real sizes (a car about 4.5, a person 1.75, a cup 0.1, a house 9) and leave space \
between objects so they do not overlap.
- Motions: "spin" turns on the spot, "bob" floats up and down a little, "hover" floats \
above the ground, "drive" moves forward (vehicles, animals, people walking), "sway" for \
plants and hanging things, "none" otherwise. Only add motion the description suggests.
- Shots: "orbit" circles the scene, "push_in" moves closer, "pull_out" moves away, "pan" \
slides sideways, "crane_up" rises, "fly_by" sweeps past, "static" holds still.
- Pick lighting and ground that match the description (a beach: sand with daylight or \
golden_hour; a city at night: concrete with night or neon; a product shot: studio).
- Descriptions are for an image generator: concrete and visual, one object each, no \
background or other objects in them."""


# -- parsing ------------------------------------------------------------------------------

_SYNONYMS: dict[str, dict[str, str]] = {
    "lighting": {
        "sunset": "golden_hour",
        "sunrise": "golden_hour",
        "dusk": "golden_hour",
        "dawn": "golden_hour",
        "golden": "golden_hour",
        "evening": "golden_hour",
        "day": "daylight",
        "sunny": "daylight",
        "sun": "daylight",
        "noon": "daylight",
        "outdoor": "daylight",
        "cloudy": "overcast",
        "rain": "overcast",
        "rainy": "overcast",
        "fog": "overcast",
        "foggy": "overcast",
        "moonlight": "night",
        "dark": "night",
        "cyberpunk": "neon",
        "synthwave": "neon",
        "club": "neon",
        "studio_light": "studio",
        "product": "studio",
        "indoor": "studio",
        "stars": "space",
        "galaxy": "space",
        "outer_space": "space",
    },
    "ground": {
        "floor": "studio",
        "backdrop": "studio",
        "white": "studio",
        "lawn": "grass",
        "field": "grass",
        "meadow": "grass",
        "forest": "grass",
        "park": "grass",
        "beach": "sand",
        "desert": "sand",
        "dunes": "sand",
        "road": "concrete",
        "street": "concrete",
        "city": "concrete",
        "asphalt": "concrete",
        "pavement": "concrete",
        "stone": "concrete",
        "rock": "concrete",
        "ocean": "water",
        "sea": "water",
        "lake": "water",
        "river": "water",
        "pool": "water",
        "ice": "snow",
        "winter": "snow",
        "space": "none",
        "sky": "none",
        "air": "none",
    },
    "shot": {
        "dolly_in": "push_in",
        "zoom_in": "push_in",
        "push": "push_in",
        "approach": "push_in",
        "dolly_out": "pull_out",
        "zoom_out": "pull_out",
        "reveal": "pull_out",
        "truck": "pan",
        "track": "pan",
        "tracking": "pan",
        "slide": "pan",
        "crane": "crane_up",
        "rise": "crane_up",
        "pedestal_up": "crane_up",
        "turntable": "orbit",
        "circle": "orbit",
        "arc": "orbit",
        "rotate": "orbit",
        "flyby": "fly_by",
        "fly_through": "fly_by",
        "sweep": "fly_by",
        "still": "static",
        "locked": "static",
        "fixed": "static",
    },
    "angle": {
        "eye_level": "eye",
        "normal": "eye",
        "front": "eye",
        "low_angle": "low",
        "worm": "low",
        "high_angle": "high",
        "above": "high",
        "birds_eye": "top",
        "overhead": "top",
        "aerial": "top",
        "top_down": "top",
    },
    "motion": {
        "rotate": "spin",
        "rotating": "spin",
        "turn": "spin",
        "spinning": "spin",
        "float": "bob",
        "floating": "hover",
        "fly": "hover",
        "flying": "hover",
        "levitate": "hover",
        "move": "drive",
        "moving": "drive",
        "walk": "drive",
        "walking": "drive",
        "run": "drive",
        "running": "drive",
        "drive": "drive",
        "driving": "drive",
        "roll": "drive",
        "rolling": "drive",
        "sail": "drive",
        "swim": "drive",
        "wave": "sway",
        "waving": "sway",
        "swaying": "sway",
        "static": "none",
        "still": "none",
        "idle": "none",
    },
    "style": {
        "photoreal": "realistic",
        "photorealistic": "realistic",
        "real": "realistic",
        "film": "cinematic",
        "movie": "cinematic",
        "dramatic": "cinematic",
        "cartoon": "stylised",
        "stylized": "stylised",
        "toon": "stylised",
        "anime": "stylised",
        "low_poly": "stylised",
        "pixar": "stylised",
        "tilt_shift": "miniature",
        "toy": "miniature",
        "diorama": "miniature",
    },
}


def _word(value: Any, allowed: tuple[str, ...], field: str, default: str) -> str:
    if not isinstance(value, str):
        return default
    key = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    if key in allowed:
        return key
    if key in _SYNONYMS.get(field, {}):
        return _SYNONYMS[field][key]
    for part in key.split("_"):  # "slow orbit", "warm sunset light"
        if part in allowed:
            return part
        if part in _SYNONYMS.get(field, {}):
            return _SYNONYMS[field][part]
    return default


def _number(value: Any, default: float, low: float, high: float) -> float:
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        value = float(match.group()) if match else None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return default
    return float(min(high, max(low, value)))


def _colour(value: Any) -> tuple[float, float, float] | None:
    """'#rrggbb' (sRGB) → linear 0..1, as Blender's colours are."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
    if not match:
        return None
    channels = [int(match.group(1)[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return (round(linear[0], 4), round(linear[1], 4), round(linear[2], 4))


def _text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()[:limit] if value is not None else ""


def _json_object(text: str) -> dict[str, Any]:
    """The first JSON object in the reply (models sometimes add prose or a fence)."""
    cleaned = re.sub(r"```(?:json)?", "", text)
    start = cleaned.find("{")
    while start != -1:
        try:
            found, _ = json.JSONDecoder().raw_decode(cleaned[start:])
        except ValueError:
            start = cleaned.find("{", start + 1)
            continue
        if isinstance(found, dict):
            return found
        start = cleaned.find("{", start + 1)
    raise PlanError("The scene planner's answer held no JSON plan.")


def _object(raw: Any) -> PlannedObject | None:
    if not isinstance(raw, dict):
        return None
    name = _text(raw.get("name"), 60)
    if not name:
        return None
    description = _text(raw.get("description"), 400) or name
    position = raw.get("position_m", raw.get("position"))
    placed: tuple[float, float] | None = None
    if isinstance(position, list | tuple) and len(position) >= 2:
        x = _number(position[0], math.nan, -MAX_DISTANCE_M, MAX_DISTANCE_M)
        y = _number(position[1], math.nan, -MAX_DISTANCE_M, MAX_DISTANCE_M)
        if math.isfinite(x) and math.isfinite(y):
            placed = (x, y)
    facing = _number(raw.get("facing_deg", raw.get("facing")), 0.0, -3600.0, 3600.0)
    facing = (facing + 180.0) % 360.0 - 180.0
    return PlannedObject(
        name=name,
        description=description,
        size_m=_number(raw.get("size_m", raw.get("size")), 1.0, MIN_SIZE_M, MAX_SIZE_M),
        position_m=placed,
        elevation_m=_number(raw.get("elevation_m", raw.get("elevation")), 0.0, 0.0, 200.0),
        facing_deg=facing,
        motion=_word(raw.get("motion"), MOTIONS, "motion", "none"),
    )


def parse_plan(text: str, *, prompt: str) -> ScenePlan:
    """The plan in the planner's reply, made valid (see the module's description)."""
    raw = _json_object(text)
    objects: list[PlannedObject] = []
    seen: dict[str, int] = {}
    for item in raw.get("objects") or []:
        found = _object(item)
        if found is None:
            continue
        key = found.name.lower()
        if key in seen:  # "tree", "tree" → "tree", "tree 2"
            seen[key] += 1
            found = found.model_copy(update={"name": f"{found.name} {seen[key]}"[:60]})
        else:
            seen[key] = 1
        objects.append(found)
        if len(objects) == MAX_OBJECTS:
            break
    if not objects:
        raise PlanError("The scene planner found nothing to model in the prompt.")
    lighting = _word(raw.get("lighting"), LIGHTINGS, "lighting", "studio")
    ground = _word(raw.get("ground"), GROUNDS, "ground", "studio")
    if lighting == "space" and ground not in {"none"}:
        ground = "none"
    names = {o.name.lower(): o.name for o in objects}
    focus = names.get(_text(raw.get("focus"), 60).lower())
    return ScenePlan(
        title=_text(raw.get("title"), 120) or _text(prompt, 120) or "Scene",
        style=_word(raw.get("style"), STYLES, "style", "cinematic"),
        lighting=lighting,
        ground=ground,
        ground_colour=_colour(raw.get("ground_colour")),
        sky_colour=_colour(raw.get("sky_colour")),
        shot=_word(raw.get("shot"), SHOTS, "shot", "orbit"),
        angle=_word(raw.get("angle"), ANGLES, "angle", "eye"),
        lens_mm=_number(raw.get("lens_mm", raw.get("lens")), 35.0, 14.0, 200.0),
        focus=focus,
        objects=objects,
    )


def with_choices(plan: ScenePlan, choices: dict[str, str | None]) -> ScenePlan:
    """The user's explicit choices (shot, lighting, ground, style, angle) win over the
    planner's; ``None`` or "auto" leaves the planner's."""
    allowed = {
        "shot": SHOTS,
        "lighting": LIGHTINGS,
        "ground": GROUNDS,
        "style": STYLES,
        "angle": ANGLES,
    }
    update = {
        field: value
        for field, value in choices.items()
        if field in allowed and value and value != "auto" and value in allowed[field]
    }
    if update.get("lighting") == "space" and "ground" not in update:
        update["ground"] = "none"
    return plan.model_copy(update=update)


# -- pictures -----------------------------------------------------------------------------

_LOOK = {
    "realistic": "photorealistic product photograph",
    "cinematic": "photorealistic, cinematic detail",
    "stylised": "stylised 3D game asset, clean shapes, vivid colours",
    "miniature": "detailed handcrafted miniature model",
}


def object_image_prompt(item: PlannedObject, style: str) -> str:
    """The reference picture TRELLIS turns into the object: the whole object alone,
    from the front, on a plain background (what image-to-3D models expect)."""
    look = _LOOK.get(style, _LOOK["cinematic"])
    return (
        f"{item.description}. A single {item.name}, the whole object in view and centred, "
        "seen from the front and slightly above, isolated on a plain light grey background, "
        f"soft even studio lighting, no other objects, no text, {look}"
    )[:1900]


# -- layout -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Footprint:
    """An object's real extent once made: width (x), depth (y), height (z), metres."""

    width: float
    depth: float
    height: float

    @property
    def radius(self) -> float:
        # Most objects do not fill their bounding box; a slightly smaller circle lets
        # them stand naturally close without touching.
        return 0.5 * math.hypot(self.width, self.depth) * 0.8


def arrange(
    objects: list[PlannedObject], footprints: list[Footprint], *, gap: float = 0.15
) -> list[tuple[float, float]]:
    """Ground positions that keep objects apart: planned positions are kept where they
    leave room; unplanned objects are set beside the others; overlaps are pushed apart
    (the smaller object moves more). ``gap`` is a fraction of the larger radius."""
    count = len(objects)
    positions: list[list[float]] = []
    order = sorted(range(count), key=lambda i: -footprints[i].radius)
    placed: list[int] = []
    for index in range(count):
        planned = objects[index].position_m
        positions.append(list(planned) if planned is not None else [math.nan, math.nan])
    for slot, index in enumerate(order):
        if objects[index].position_m is not None:
            placed.append(index)
            continue
        if not placed:
            positions[index] = [0.0, 0.0]
        else:
            # Alternate right, left, behind-right, behind-left of what is there.
            side = 1 if slot % 2 == 1 else -1
            reach = max(abs(positions[i][0]) + footprints[i].radius for i in placed)
            behind = 0.0 if slot <= 2 else footprints[index].radius * 1.5
            positions[index] = [side * (reach + footprints[index].radius) * (1 + gap), behind]
        placed.append(index)

    for _ in range(60):
        moved = False
        for a in range(count):
            for b in range(a + 1, count):
                fa, fb = footprints[a], footprints[b]
                ea, eb = objects[a].elevation_m, objects[b].elevation_m
                if abs(ea - eb) >= max(fa.height, fb.height):  # one flies above the other
                    continue
                need = (fa.radius + fb.radius) * (1 + gap)
                dx = positions[b][0] - positions[a][0]
                dy = positions[b][1] - positions[a][1]
                dist = math.hypot(dx, dy)
                if dist >= need:
                    continue
                if dist < 1e-6:
                    dx, dy, dist = 1.0, 0.0, 1.0
                push = need - dist
                wa = fb.radius / (fa.radius + fb.radius)  # the smaller one moves more
                positions[a][0] -= dx / dist * push * wa
                positions[a][1] -= dy / dist * push * wa
                positions[b][0] += dx / dist * push * (1 - wa)
                positions[b][1] += dy / dist * push * (1 - wa)
                moved = True
        if not moved:
            break
    return [(round(x, 3), round(y, 3)) for x, y in positions]


# -- render settings ----------------------------------------------------------------------

RENDER_PRESETS: dict[QualityMode, dict[str, Any]] = {
    QualityMode.FAST: {
        "width": 1280,
        "height": 720,
        "fps": 24,
        "engine": "eevee",
        "samples": 16,
        "still_engine": "eevee",
        "still_samples": 64,
        "motion_blur": False,
        "raytracing": False,
    },
    QualityMode.HIGH_QUALITY: {
        "width": 1920,
        "height": 1080,
        "fps": 24,
        "engine": "eevee",
        "samples": 48,
        "still_engine": "eevee",
        "still_samples": 128,
        "motion_blur": True,
        "raytracing": False,
    },
    QualityMode.PROFESSIONAL: {
        "width": 1920,
        "height": 1080,
        "fps": 24,
        "engine": "eevee",
        "samples": 96,
        "still_engine": "cycles",
        "still_samples": 128,
        "motion_blur": True,
        "raytracing": True,
    },
}

# Reference pictures and objects per profile: FLUX.1-schnell steps, then the object's
# model as for the GPU server provider (TRELLIS steps; triangles and texture kept).
OBJECT_PRESETS: dict[QualityMode, dict[str, int]] = {
    QualityMode.FAST: {"image_steps": 4, "image_size": 768, "shape_steps": 12},
    QualityMode.HIGH_QUALITY: {"image_steps": 4, "image_size": 1024, "shape_steps": 25},
    QualityMode.PROFESSIONAL: {"image_steps": 6, "image_size": 1024, "shape_steps": 50},
}
