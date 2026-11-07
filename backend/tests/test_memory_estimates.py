"""The memory estimates that decide whether a check, levels of detail or an export
may run stay above the peaks measured on real runs (docs/BENCHMARKS.md, 2026-11-07,
infrastructure/scripts/benchmark_geometry.py; peak resident memory of the process,
in millions of bytes).

The counts are those read from each model's GLB header: triangles, vertices,
vertices with texture coordinates, decoded texture bytes."""

from __future__ import annotations

import pytest

from ai3d.models.enums import ExportFormat
from ai3d.processing import lod
from ai3d.processing.geometry import smoothing_memory_bytes
from ai3d.processing.mesh import GlbCounts, validation_memory_bytes
from ai3d.services import inspection
from ai3d.services.exports import export_memory_bytes

TEXTURE = 2048 * 2048 * 4 * 2  # the synthetic terrains' 2048 px texture, decoded


def _terrain(triangles: int, vertices: int, textured: bool = True) -> GlbCounts:
    return GlbCounts(triangles, vertices, vertices if textured else 0, TEXTURE if textured else 0)


def _plain(triangles: int, vertices: int) -> GlbCounts:
    return GlbCounts(triangles, vertices, 0, 0)


T1, T3, T5 = (
    _terrain(999_698, 501_264),
    _terrain(3_001_250, 1_503_076),
    _terrain(4_999_122, 2_502_724),
)
T15, T20 = _terrain(15_004_242, 7_507_600), _terrain(19_996_488, 10_004_569)
DRAGON, STATUETTE = _plain(7_219_045, 3_609_600), _plain(10_000_000, 4_999_996)
BARE1, BARE5 = _plain(999_698, 501_264), _plain(4_999_122, 2_502_724)
SOUP1, SOUP5 = _plain(999_698, 2_999_094), _plain(4_999_122, 14_997_366)
PARTS1, PARTS5 = _plain(999_663, 542_925), _plain(4_999_043, 2_595_863)

# (model, measured peak in MB). Colours: same counts as bare, plus normals/colours.
CHECK = [
    (T1, 847), (T3, 1356), (T5, 2024), (T15, 3147), (T20, 4120), (DRAGON, 2828),
    (STATUETTE, 3714), (BARE1, 732), (BARE1, 748), (BARE5, 1960), (BARE5, 1992),
    (SOUP1, 1120), (SOUP5, 2992), (PARTS1, 746), (PARTS5, 1857),
]  # fmt: skip
LEVELS = [
    (T1, 1181), (T3, 1984), (T5, 2995), (T15, 4213), (T20, 4128), (DRAGON, 3218),
    (STATUETTE, 3622), (BARE1, 1141), (BARE1, 1098), (BARE5, 2912), (BARE5, 2905),
    (SOUP1, 1435), (SOUP5, 4200), (PARTS1, 227), (PARTS5, 583),
]  # fmt: skip
# The largest of GLB, OBJ and STL (FBX runs in Blender's own process).
EXPORT = [
    (T1, 1248), (T3, 2941), (T5, 4561), (DRAGON, 4972), (STATUETTE, 5714),
    (BARE1, 711), (BARE1, 795), (BARE5, 2773), (BARE5, 2965), (SOUP1, 1621),
    (SOUP5, 4421), (PARTS1, 614), (PARTS5, 2353),
]  # fmt: skip


# GLB (2026-11-07, the current code; the 5M soup with the earlier code, which kept
# the model loaded while checking, an upper bound).
GLB = [(T1, 431), (T5, 1122), (STATUETTE, 1655), (T15, 3365), (T20, 4223), (SOUP5, 3476)]
# FBX: the whole process tree, Blender included (sampled every 0.2 s), largest of the
# runs on the same model.
FBX = [(T1, 2282), (T5, 7560), (STATUETTE, 8623)]


@pytest.mark.parametrize(("fmt", "runs"), [(ExportFormat.GLB, GLB), (ExportFormat.FBX, FBX)])
def test_glb_and_fbx_export_estimates_are_above_every_measured_peak(
    fmt: ExportFormat, runs: list[tuple[GlbCounts, int]]
) -> None:
    for counts, peak_mb in runs:
        assert export_memory_bytes(counts, fmt) > 1.05 * peak_mb * 1e6, (counts, peak_mb)


def test_the_glb_estimate_lets_large_models_export_that_fit() -> None:
    # A textured 20M terrain was exported (4.2 GB); the shared estimate asked for 22 GB.
    assert export_memory_bytes(T20, ExportFormat.GLB) < 6e9
    assert export_memory_bytes(T20) > 20e9


@pytest.mark.parametrize(
    ("estimate", "runs"),
    [
        (validation_memory_bytes, CHECK),
        (lod.memory_bytes, LEVELS),
        (export_memory_bytes, EXPORT),
    ],
    ids=["check", "levels", "export"],
)
def test_estimates_are_above_every_measured_peak(estimate, runs) -> None:  # type: ignore[no-untyped-def]
    for counts, peak_mb in runs:
        assert estimate(counts) > peak_mb * 1e6, (counts, peak_mb)


def test_unshared_vertices_raise_the_estimates() -> None:
    # A soup (three vertices per triangle) needs more than the same triangles shared.
    assert validation_memory_bytes(SOUP5) > validation_memory_bytes(BARE5)
    assert lod.memory_bytes(SOUP5) > lod.memory_bytes(BARE5)
    assert export_memory_bytes(SOUP5) > export_memory_bytes(BARE5)


# Smoothing: memory above the loaded model (peak minus what was in use before), 10
# passes; (triangles, MB) measured 2026-11-07.
SMOOTHING = {
    "sharp": [(1_002_252, 449), (4_992_300, 1168), (10_002_828, 2039), (7_219_045, 1614),
              (10_000_000, 1749)],
    "taubin": [(1_002_252, 268), (4_992_300, 946), (10_002_828, 1712), (7_219_045, 1227),
               (10_000_000, 1748)],
}  # fmt: skip


@pytest.mark.parametrize("method", ["sharp", "taubin"])
def test_smoothing_estimates_are_above_every_measured_run(method: str) -> None:
    for triangles, extra_mb in SMOOTHING[method]:
        assert smoothing_memory_bytes(triangles, method) > 1.15 * extra_mb * 1e6  # type: ignore[arg-type]


# Inspection (2026-11-08, the current code): the highest peak of the default, printing
# and full self-intersection runs on each model. Peaks of the same run differ by up to
# 1 GB between runs (macOS compresses idle memory), so the highest one seen is used.
CUBE = 786_432  # the noisy cubes' small texture, decoded
INSPECT = [
    (T1, 975), (T5, 2169), (DRAGON, 2563), (STATUETTE, 4941),
    (GlbCounts(1_002_252, 504_600, 504_600, CUBE), 791),
    (GlbCounts(4_992_300, 2_503_896, 2_503_896, CUBE), 2937),
    (GlbCounts(10_002_828, 5_012_376, 5_012_376, CUBE), 4381),
]  # fmt: skip


def test_inspection_estimate_is_above_every_measured_peak() -> None:
    for counts, peak_mb in INSPECT:
        assert inspection.memory_bytes(counts) > 1.05 * peak_mb * 1e6, (counts, peak_mb)
    # Unshared vertices (three per triangle) need more.
    assert inspection.memory_bytes(SOUP5) > inspection.memory_bytes(BARE5)
