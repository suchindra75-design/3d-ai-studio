# Benchmarks: large models

Measured on 2026-11-06. Every number here comes from a run of the scripts
below on the machine described; nothing is estimated or extrapolated. Where a
number depends on how it was measured, the method says how.

## Machine

- MacBook Pro 16" (2019), Intel Core i7-9750H, 6 cores / 12 threads, 2.6 GHz
- 16 GB memory
- AMD Radeon Pro 5300M (4 GB) and Intel UHD Graphics 630
- macOS 26.7.1, Python 3.11, Blender 4.5.15 LTS, Chromium from Playwright

## Models

| Model | Triangles | Kind | GLB size |
|---|---|---|---|
| Terrain 1M | 999,698 | synthetic: a bumpy heightfield with a 2048 px texture, made by the script | 34.6 MB |
| Terrain 3M | 3,001,250 | synthetic, as above | 78.7 MB |
| Terrain 5M | 4,999,122 | synthetic, as above | 122.6 MB |
| XYZ RGB Dragon | 7,219,045 | **real 3D scan** (Stanford 3D Scanning Repository), geometry only | 129.9 MB |
| XYZ RGB Thai Statuette | 10,000,000 | **real 3D scan** (Stanford 3D Scanning Repository), geometry only | 180.0 MB |

The two scans were downloaded for these runs with permission (research use;
not redistributed or committed). They were imported through the app's own PLY
import, as a user would.

## Backend (`infrastructure/scripts/benchmark_geometry.py`)

Each step runs in its own process; "peak" is that process's maximum resident
memory, including loading the model.

```bash
uv run python infrastructure/scripts/benchmark_geometry.py OUT synthetic:1000000 synthetic:3000000 \
  synthetic:5000000 xyzrgb_dragon.ply xyzrgb_statuette.ply
```

Seconds (peak memory in GB):

| Step | 1M | 3M | 5M | Dragon 7.2M | Statuette 10M |
|---|---|---|---|---|---|
| Import (PLY → GLB) | 1.1 (0.3) | 1.6 (0.6) | 2.2 (0.8) | 1.3 (1.0) | 1.9 (1.3) |
| Upload check (`validate_mesh_file`) | 3.7 (1.0) | 8.7 (2.1) | 15.8 (3.3) | 23.9 (4.4) | 47.3 (**8.4**) |
| Levels of detail (all levels) | 6.4 (1.3) | 21.5 (3.3) | 37.8 (5.3) | 52.5 (6.3) | 100.4 (6.6) |
| Smoothing, keep sharp edges (10 passes) | 29.7 (1.3) | 103.4 (3.3) | 169.7 (5.4) | 243.7 (7.2) | 389.1 (**8.4**) |
| Smoothing, Taubin (10 passes) | 2.2 (0.6) | 7.7 (1.3) | 12.2 (2.1) | 21.7 (2.7) | 56.8 (5.8) |
| Export GLB (write + re-open) | 3.1 (0.5) | 4.4 (0.9) | 6.6 (1.4) | 7.0 (1.8) | 9.2 (2.1) |
| Export OBJ | 18.8 (1.2) | 53.9 (2.9) | 88.7 (4.6) | 83.3 (3.2) | 117.8 (4.8) |
| Export STL | 3.4 (1.1) | 8.8 (2.3) | 11.4 (3.6) | 16.7 (5.0) | 22.4 (5.7) |
| Export FBX (Blender) | 17.5 (0.4) | 37.2 (0.7) | 60.3 (1.1) | 81.1 (1.2) | 111.4 (1.6) |

Levels of detail built (triangles; seconds for that level):

| Model | Level 1 | Level 2 | Level 3 |
|---|---|---|---|
| 1M | 249,924 (5.1) | 62,480 (1.1) | — |
| 3M | 750,312 (16.6) | 187,578 (3.2) | 46,894 (1.1) |
| 5M | 1,249,780 (29.2) | 312,444 (5.6) | 78,110 (1.9) |
| Dragon | 1,804,761 (41.0) | 451,189 (7.9) | 112,797 (2.3) |
| Statuette | 2,500,000 (77.9) | 625,000 (14.7) | 156,250 (3.8) |

Every export was re-opened and matched the model's triangle count.

**What this shows:**
- Everything completed on 16 GB, up to the 10-million-triangle scan.
- The upload check and feature-preserving smoothing both peak at 8.4 GB on
  10 million triangles, about half this machine's memory. Smoothing now checks
  free memory first (700 bytes per triangle) and says so instead of running out;
  the upload check has no such guard yet (a known gap below).
- Keeping sharp edges costs 7–14× Taubin's time; on 10 million triangles it
  takes six and a half minutes.
- OBJ export is the slowest writer (trimesh's text writer), about 12 s per
  million triangles.

## Browser (`e2e/benchmarks/large-models.spec.ts`)

```bash
AI3D_BENCHMARK_GLBS=/path/a.glb,/path/b.glb \
  pnpm exec playwright test e2e/benchmarks --project=desktop --workers=1
```

Through the web app. The headless Chromium drew with **ANGLE on Metal, Intel UHD Graphics 630** (the
integrated GPU, not the Radeon). Times include the backend, which ran on the
same machine.

| Model | Upload + check | Levels built | First view | Full model shown | JS heap after load | Auto level when framed |
|---|---|---|---|---|---|---|
| 1M | 5.6 s | 7.1 s | 6.4 s | 8.0 s | 143 MB | Level 2 (62K) |
| 3M | 11.7 s | 22.7 s | 4.1 s | 8.1 s | 295 MB | Level 1 (750K) |
| 5M | 18.0 s | 36.4 s | 5.5 s | 10.7 s | 445 MB | Level 2 (312K) |
| Dragon 7.2M | 29.1 s | 53.0 s | 8.0 s | 11.1 s | 479 MB | Level 2 (451K) |
| Statuette 10M | 53.6 s | 90.6 s | 8.7 s | 13.6 s | 652 MB | Level 2 (625K) |

- *First view* is the coarsest level shown as a labelled preview (the levels
  were built before opening); *full model shown* is when the full file has
  loaded and replaced it. Both count from opening the page.
- All five open and stay usable in a 1440 × 900 window, the 10-million-triangle
  scan included; the JS heap stays under 0.7 GB.
- When the model is framed, Auto draws a level, not full detail, for every
  model; zoomed in close it switches to full detail (checked in
  `e2e/lod.spec.ts`).

**Rendering speed was not measured validly.** A first version of this test
orbited the camera with the frame-rate limit and vsync off and timed each step:
27–28 ms for every level, full detail on 10 million triangles included. That
method (move the mouse, wait for two animation frames) shows how quickly the
browser accepts frames, not how long the GPU takes to draw them, so the numbers
say nothing about the levels; the timing was removed from the test. A valid
measurement needs GPU timer queries or the browser's GPU trace; that is a
known gap. What the levels do is certain: the triangles submitted per frame
drop to the level's count (a quarter or less), as the Detail control reports.

## Known gaps

- The upload check (`validate_mesh_file`) peaks at 8.4 GB on 10 million
  triangles with no memory check before it starts.
- Rendering speed per level of detail (see above).
- Apple Silicon, Windows, Linux and discrete-GPU browsers not measured.
