"""Scene plans: reading a language model's reply leniently, the user's choices, and
laying the objects out on the ground."""

import json
import math
from typing import Any

import pytest

from ai3d.processing.scene_plan import (
    LIGHTINGS,
    MAX_OBJECTS,
    MOTIONS,
    PLANNER_INSTRUCTIONS,
    SHOTS,
    Footprint,
    PlanError,
    PlannedObject,
    arrange,
    object_image_prompt,
    parse_plan,
    with_choices,
)


def _reply(**fields: Any) -> str:
    plan = {
        "title": "Beach day",
        "style": "cinematic",
        "lighting": "golden_hour",
        "ground": "sand",
        "shot": "orbit",
        "angle": "eye",
        "lens_mm": 35,
        "focus": "red car",
        "objects": [
            {
                "name": "red car",
                "description": "a glossy red sports car",
                "size_m": 4.5,
                "position_m": [0, 0],
                "facing_deg": 30,
                "motion": "drive",
            },
            {"name": "palm tree", "description": "a tall palm", "size_m": 8, "motion": "sway"},
        ],
    } | fields
    return json.dumps(plan)


def test_a_clean_reply_is_read_as_is() -> None:
    plan = parse_plan(_reply(), prompt="A red car on a beach at sunset")
    assert plan.title == "Beach day"
    assert (plan.lighting, plan.ground, plan.shot, plan.style) == (
        "golden_hour",
        "sand",
        "orbit",
        "cinematic",
    )
    car, palm = plan.objects
    assert car.position_m == (0.0, 0.0)
    assert car.facing_deg == 30
    assert car.motion == "drive"
    assert palm.position_m is None  # the layout will choose
    assert plan.focus == "red car"


def test_json_is_found_inside_prose_and_code_fences() -> None:
    text = f"Sure! Here is the plan:\n```json\n{_reply()}\n```\nEnjoy."
    assert parse_plan(text, prompt="x").objects[0].name == "red car"


@pytest.mark.parametrize(
    ("field", "said", "meant"),
    [
        ("lighting", "Sunset", "golden_hour"),
        ("lighting", "warm sunset light", "golden_hour"),
        ("lighting", "cyberpunk", "neon"),
        ("ground", "beach", "sand"),
        ("ground", "city street", "concrete"),
        ("shot", "dolly in", "push_in"),
        ("shot", "slow turntable", "orbit"),
        ("style", "Pixar", "stylised"),
        ("style", "photorealistic", "realistic"),
        ("lighting", "something odd", "studio"),  # unknown: the default
    ],
)
def test_near_miss_words_become_known_values(field: str, said: str, meant: str) -> None:
    plan = parse_plan(_reply(**{field: said}), prompt="x")
    assert getattr(plan, field) == meant


def test_numbers_are_clamped_and_motions_mapped() -> None:
    reply = _reply(
        lens_mm=5000,
        objects=[
            {"name": "ant", "size_m": 0.0001, "motion": "walking"},
            {"name": "moon", "size_m": 3_000_000, "motion": "floating", "facing_deg": 540},
            {"name": "drone", "size_m": "about 0.5 m", "elevation_m": -3, "motion": "fly"},
        ],
    )
    plan = parse_plan(reply, prompt="x")
    ant, moon, drone = plan.objects
    assert plan.lens_mm == 200
    assert ant.size_m == 0.03
    assert ant.motion == "drive"
    assert moon.size_m == 60
    assert moon.motion == "hover"
    assert moon.facing_deg == -180
    assert drone.size_m == 0.5
    assert drone.elevation_m == 0
    assert drone.motion == "hover"
    assert ant.description == "ant"  # missing description: the name


def test_duplicates_are_numbered_and_extra_objects_dropped() -> None:
    objects = [{"name": "tree", "size_m": 5}] * (MAX_OBJECTS + 3)
    plan = parse_plan(_reply(objects=objects), prompt="x")
    assert len(plan.objects) == MAX_OBJECTS
    assert [o.name for o in plan.objects[:3]] == ["tree", "tree 2", "tree 3"]


def test_space_has_no_ground() -> None:
    plan = parse_plan(_reply(lighting="outer space", ground="grass"), prompt="x")
    assert (plan.lighting, plan.ground) == ("space", "none")


def test_colours_are_hex_sRGB_turned_linear() -> None:
    plan = parse_plan(_reply(ground_colour="#808080", sky_colour="blue"), prompt="x")
    assert plan.ground_colour is not None
    assert plan.ground_colour[0] == pytest.approx(0.2159, abs=1e-3)
    assert plan.sky_colour is None


@pytest.mark.parametrize(
    "text",
    ["no json here", '{"objects": []}', '{"objects": [{"size_m": 3}]}', "[1, 2, 3]"],
)
def test_a_reply_without_objects_is_refused(text: str) -> None:
    with pytest.raises(PlanError):
        parse_plan(text, prompt="x")


def test_the_instructions_name_every_value_the_renderer_knows() -> None:
    for value in (*LIGHTINGS, *SHOTS, *MOTIONS):
        assert value in PLANNER_INSTRUCTIONS
    assert str(MAX_OBJECTS) in PLANNER_INSTRUCTIONS


def test_the_users_choices_win_over_the_planners() -> None:
    plan = parse_plan(_reply(), prompt="x")
    chosen = with_choices(plan, {"shot": "crane_up", "lighting": "auto", "style": None})
    assert chosen.shot == "crane_up"
    assert chosen.lighting == "golden_hour"  # auto: the planner's
    assert chosen.style == "cinematic"
    space = with_choices(plan, {"lighting": "space"})
    assert space.ground == "none"
    assert with_choices(plan, {"shot": "spiral"}).shot == "orbit"  # unknown: ignored


def test_the_reference_picture_asks_for_the_object_alone() -> None:
    item = PlannedObject(name="robot", description="a friendly silver robot", size_m=1.6)
    prompt = object_image_prompt(item, "stylised")
    assert prompt.startswith("a friendly silver robot. A single robot")
    assert "plain light grey background" in prompt
    assert "stylised 3D game asset" in prompt


def _gaps(
    objects: list[PlannedObject], prints: list[Footprint], places: list[tuple[float, float]]
) -> list[float]:
    """For every pair standing at the same height: distance minus the two radii."""
    gaps = []
    for a in range(len(objects)):
        for b in range(a + 1, len(objects)):
            (xa, ya), (xb, yb) = places[a], places[b]
            gaps.append(math.hypot(xb - xa, yb - ya) - prints[a].radius - prints[b].radius)
    return gaps


def test_unplaced_objects_are_set_beside_each_other_without_touching() -> None:
    objects = [
        PlannedObject(name=n, description=n, size_m=s)
        for n, s in (("house", 9), ("car", 4.5), ("dog", 0.8), ("tree", 6))
    ]
    prints = [
        Footprint(9, 8, 7),
        Footprint(1.9, 4.5, 1.4),
        Footprint(0.3, 0.8, 0.6),
        Footprint(4, 4, 6),
    ]
    places = arrange(objects, prints)
    assert places[0] == (0.0, 0.0)  # the largest in the middle
    assert min(_gaps(objects, prints, places)) > 0


def test_planned_places_are_kept_when_free_and_pushed_apart_when_not() -> None:
    free = [
        PlannedObject(name="a", description="a", size_m=1, position_m=(-5, 0)),
        PlannedObject(name="b", description="b", size_m=1, position_m=(5, 2)),
    ]
    prints = [Footprint(1, 1, 1), Footprint(1, 1, 1)]
    assert arrange(free, prints) == [(-5.0, 0.0), (5.0, 2.0)]

    clash = [
        PlannedObject(name="big", description="big", size_m=4, position_m=(0, 0)),
        PlannedObject(name="small", description="small", size_m=1, position_m=(0.5, 0)),
    ]
    prints = [Footprint(4, 4, 3), Footprint(1, 1, 1)]
    places = arrange(clash, prints)
    assert min(_gaps(clash, prints, places)) > 0
    # The small one moved more.
    assert abs(places[1][0] - 0.5) > abs(places[0][0])


def test_a_flying_object_may_be_above_another() -> None:
    objects = [
        PlannedObject(name="car", description="car", size_m=4, position_m=(0, 0)),
        PlannedObject(
            name="drone", description="drone", size_m=0.5, position_m=(0, 0), elevation_m=5
        ),
    ]
    places = arrange(objects, [Footprint(2, 4, 1.5), Footprint(0.5, 0.5, 0.2)])
    assert places == [(0.0, 0.0), (0.0, 0.0)]
