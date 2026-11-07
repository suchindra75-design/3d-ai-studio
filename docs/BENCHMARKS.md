# Benchmarks: large models

Measured on 2026-11-06 and 2026-11-07 on one machine (below). Every number
here comes from a run of the scripts named in each section; nothing is
estimated or extrapolated, and where a number depends on how it was measured
the method says how. Other platforms: see [HARDWARE_TESTING.md](HARDWARE_TESTING.md);
none has been measured.

**Correction (2026-11-07).** The first version of this page (2026-11-06) said
the browser drew "with Intel UHD Graphics 630". That was wrong: the app asks
WebGL for the high-performance GPU, and the GPU name had been read from a
separate test canvas, which Chrome gave the integrated GPU. Those runs drew on
the Radeon. The method is fixed (the GPU is now read from the app's own
context, and Chrome is told which GPU to use) and the tables below name the
GPU each run used.

## Machine

- MacBook Pro 16" (2019), Intel Core i7-9750H, 6 cores / 12 threads, 2.6 GHz
- 16 GB memory
- AMD Radeon Pro 5300M (4 GB) and Intel UHD Graphics 630
- macOS 26.7.1, Python 3.11, Chrome 156 (the installed browser, driven by
  Playwright), Blender 4.5.15 LTS
- **Low Power Mode was on for every 2026-11-07 run** (it cannot be switched off
  from these tools), mostly on battery. CPU-bound times are probably slower than
  this machine can do; they are comparable with each other, not with a machine
  at full power.
- Backend, web app (Next.js development server) and browser all ran on this
  machine.

## Models

| Model | Triangles | Vertices | Kind | GLB size |
|---|---|---|---|---|
| Terrain 1M | 999,698 | 501,264 | synthetic heightfield with a 2048 px texture (`synthetic:<n>`) | 34.6 MB |
| Terrain 3M | 3,001,250 | 1,503,076 | as above | 78.7 MB |
| Terrain 5M | 4,999,122 | 2,502,724 | as above | 122.6 MB |
| Terrain 15M | 15,004,242 | 7,507,600 | as above, with normals | 432.9 MB |
| Terrain 20M | 19,996,488 | 10,004,569 | as above, with normals | 572.7 MB |
| XYZ RGB Dragon | 7,219,045 | 3,609,600 | **real 3D scan**, geometry only | 129.9 MB |
| XYZ RGB Thai Statuette | 10,000,000 | 4,999,996 | **real 3D scan**, geometry only | 180.0 MB |

Other kinds, each at 1 and 5 million triangles (`synthetic:<n>:<kind>`):
**colours** (vertex colours and normals, no texture), **soup** (no shared
vertices: three per triangle, normals), **parts** (400 separate meshes, 20 × 20
blocks), **bare** (positions only: no normals, colours or texture).

The two scans come from the [Stanford 3D Scanning Repository](http://graphics.stanford.edu/data/3Dscanrep/)
(research use). They were downloaded with permission for these runs, imported
through the app's own PLY import, and are not committed or redistributed.

## Backend (`infrastructure/scripts/benchmark_geometry.py`)

Each step runs in its own process; "peak" is that process's maximum resident
memory (in GB of 10⁹ bytes), loading the model included.

```bash
uv run python infrastructure/scripts/benchmark_geometry.py OUT synthetic:1000000 synthetic:3000000 \
  synthetic:5000000 xyzrgb_dragon.ply xyzrgb_statuette.ply
uv run python infrastructure/scripts/benchmark_geometry.py OUT --steps import,validate,lod,export_glb,export_obj \
  synthetic:1000000:soup synthetic:5000000:soup   # and :colours, :parts, :bare
```

### Upload check: 8.4 → 3.7 GB on 10 million triangles

The check that every upload goes through (D61). Seconds (peak GB):

| Model | First version (2026-11-06) | Chunked (`f3a0f84`) | Staged (now) |
|---|---|---|---|
| Terrain 1M | 3.7 (1.0) | 3.3 (0.9) | 3.0 (0.85) |
| Terrain 3M | 8.7 (2.1) | 8.5 (1.7) | 6.3 (1.36) |
| Terrain 5M | 15.8 (3.3) | 14.8 (2.8) | 11.3 (2.02) |
| Dragon 7.2M | 23.9 (4.4) | 23.0 (3.5) | 18.6 (2.83) |
| Statuette 10M | 47.3 (**8.4**) | 34.8 (4.7) | 29.1 (**3.71**) |
| Terrain 15M | — | — | 39.6 (3.15) |
| Terrain 20M | — | — | 55.8 (4.12), see below |

How: the check now runs in three stages (scene facts and textures; positions;
topology on the merged shape) and frees each stage's data before the next;
edge use, watertightness and winding come from one sort of integer edge keys
instead of trimesh's cached adjacency. Every metric was compared with the
previous code on the same files (inside-out, open, soup, non-manifold,
duplicates, textures, parts, the 10M scan): identical. Above 10 million
triangles the count of separate pieces is skipped (reported as skipped, not as
zero), which is why 15M needs less than the 10M scan.

### Levels of detail: 6.6 → 3.6 GB on 10 million triangles

Levels are now simplified in pieces of at most 2.5 million triangles (D62) and
built in a separate process in the background (D65). Seconds (peak GB):

| Model | Before pieces | Now | Levels (triangles) | Mean shift of level 1 |
|---|---|---|---|---|
| Terrain 1M | 6.4 (1.3) | 8.1 (1.18) | 249,924 · 62,480 | 0.12% |
| Terrain 3M | 21.5 (3.3) | 26.5 (1.98) | 750,310 · 187,578 · 46,894 | 0.07% |
| Terrain 5M | 37.8 (5.3) | 46.9 (3.00) | 1,249,778 · 312,444 · 78,110 | 0.06% |
| Dragon 7.2M | 52.5 (6.3) | 54.0 (3.22) | 1,804,759 · 451,189 · 112,797 | 0.04% |
| Statuette 10M | 100.4 (6.6) | 80.1 (3.62) | 2,499,998 · 625,000 · 156,250 · 39,062 | 0.03% |
| Terrain 15M | — | 109.0 (4.21) | 3,751,054 · 937,764 · 234,440 · 58,610 | 0.03% |
| Terrain 20M | — | 172.1 (4.13) | 4,999,116 · 1,249,778 · 312,444 · 78,110 | 0.03% |

"Mean shift" is how far the original vertices are from the simplified surface,
as a share of the model's diagonal; pieces did not change it (Statuette level 1:
0.031% either way). The smaller models got 3–27% slower; the "before" column was
measured on 2026-11-06, the "now" column with Low Power Mode on, and the two
causes were not separated.

### Other kinds of model

Seconds (peak GB). Every export was re-opened and matched the triangle count.

| Model | Import | Check | Levels | GLB export | OBJ export |
|---|---|---|---|---|---|
| Colours 1M | 0.5 (0.28) | 3.4 (0.75) | 6.1 (1.10), 2 levels | 3.2 (0.39) | 14.7 (0.80) |
| Soup 1M | 1.7 (0.64) | 6.0 (1.12) | 10.2 (1.44), 2 levels | 4.9 (0.90) | 29.9 (1.62) |
| Parts 1M | 0.2 (0.20) | 3.3 (0.75) | 3.9 (0.23), 1 level | 3.7 (0.26) | 14.1 (0.61) |
| Bare 1M | 0.2 (0.23) | 2.8 (0.73) | 7.3 (1.14), 2 levels | 2.1 (0.35) | 11.6 (0.71) |
| Colours 5M | 2.5 (0.92) | 14.7 (1.99) | 36.1 (2.91), 3 levels | 11.4 (1.40) | 71.0 (2.97) |
| Soup 5M | 10.9 (2.18) | 31.8 (2.99) | 60.1 (4.20), 3 levels | 22.5 (3.48) | 150.8 (4.42) |
| Parts 5M | 0.9 (0.43) | 12.8 (1.86) | 19.3 (0.58), 2 levels | 5.0 (0.74) | 51.3 (2.35) |
| Bare 5M | 0.8 (0.66) | 10.5 (1.96) | 37.2 (2.91), 3 levels | 4.8 (1.30) | 52.2 (2.77) |

- **Soups cost the most:** three vertices per triangle make the GLB 420 MB at 5
  million triangles (the upload limit is 500 MB) and the OBJ export peak 4.4 GB.
- **Parts** are simplified one mesh at a time, so levels need little memory. A
  level that would keep more than 90% of the triangles is now dropped: a first
  version of this generator made disconnected triangles, which the simplifier
  could not reduce at all, and the app stored "levels" as large as the model.
  Such a model now says it "could not be simplified usefully" instead.
- Colours and bare models behave like the textured terrain.

### Memory estimates against the measurements

Before the check, levels and exports, the memory needed is estimated from the
GLB header (no geometry loaded) and the work is refused, with the numbers, if
it would not fit in free memory. Estimates made from triangles alone were
**below the measured peak on 8 runs**: the check and the levels for soups, and
OBJ exports of textured models and soups. They now count vertices and textured
vertices too, and `backend/tests/test_memory_estimates.py` checks each
estimate against every run in the tables above:

| Estimate | Formula | Smallest margin over a measured peak |
|---|---|---|
| Check | 600 MiB + 300 B/triangle + 90 B/vertex + decoded textures | 7% (Soup 1M) |
| Levels | 1 GiB + 450 B per triangle up to 2.5M + 120 B/triangle + 130 B/vertex + textures | 5% (Terrain 5M) |
| Export | 700 MiB + 600 B/triangle + 150 B/vertex + 800 B/textured vertex + textures | 10% (Soup 1M, OBJ) |

They are deliberately cautious where the measurements stop: the 20M terrain's
check is estimated at 7.6 GB and measured at 4.1 GB (it was refused on this
machine at the time, and was measured with the guard bypassed); its levels are
estimated at 5.9 GB (measured 4.1 GB); its export, never measured, is estimated
at 22 GB and so is refused on a 16 GB machine. A refusal says how much is
needed and how much is free; nothing is recorded, and it can be tried again.

### Earlier steps (2026-11-06, unchanged since)

| Step | 1M | 3M | 5M | Dragon 7.2M | Statuette 10M |
|---|---|---|---|---|---|
| Import (PLY → GLB) | 1.1 (0.3) | 1.6 (0.6) | 2.2 (0.8) | 1.3 (1.0) | 1.9 (1.3) |
| Smoothing, keep sharp edges (10 passes) | 29.7 (1.3) | 103.4 (3.3) | 169.7 (5.4) | 243.7 (7.2) | 389.1 (8.4) |
| Smoothing, Taubin (10 passes) | 2.2 (0.6) | 7.7 (1.3) | 12.2 (2.1) | 21.7 (2.7) | 56.8 (5.8) |
| Export GLB (write + re-open) | 3.1 (0.5) | 4.4 (0.9) | 6.6 (1.4) | 7.0 (1.8) | 9.2 (2.1) |
| Export OBJ | 18.8 (1.2) | 53.9 (2.9) | 88.7 (4.6) | 83.3 (3.2) | 117.8 (4.8) |
| Export STL | 3.4 (1.1) | 8.8 (2.3) | 11.4 (3.6) | 16.7 (5.0) | 22.4 (5.7) |
| Export FBX (Blender) | 17.5 (0.4) | 37.2 (0.7) | 60.3 (1.1) | 81.1 (1.2) | 111.4 (1.6) |

Feature-preserving smoothing still peaks at 8.4 GB on 10 million triangles; it
checks free memory first (700 bytes per triangle) and was not reworked in this
milestone.

## Rendering (`e2e/benchmarks/rendering.spec.ts`)

```bash
AI3D_BENCHMARK_GLBS=OUT/xyzrgb_statuette/model.glb AI3D_BENCHMARK_GPU=integrated \
  pnpm exec playwright test e2e/benchmarks/rendering.spec.ts --project=desktop --workers=1
```

**Method.** The test opens the model in the app (1440 × 900, device pixel ratio
1), fixes the Detail control at each level in turn, and times the app's own
`renderer.render()` call: GPU time with WebGL2 timer queries
(`EXT_disjoint_timer_query_webgl2`), CPU time (preparing and submitting the
draw) with `performance.now()` around the call, and draw calls and triangles
from the renderer. 20 warm-up frames, then 60 measured frames per level;
medians, 90th/95th percentiles and maxima are reported. Every result had 60 GPU
samples and 0 disjoint (invalid) timer results. Chrome was told which GPU to
use (`--force_low_power_gpu` / `--force_high_performance_gpu`) and the GPU name
was read from the app's context. The hook is inert unless a test creates it
(D63). The viewer has no post-processing pass, so there is nothing to switch
off there; shadows are measured off and on.

GPU time per frame, median in ms (95th percentile):

**Intel UHD Graphics 630 (integrated)**

| Model | Full detail | Level 1 | Level 2 | Level 3 | Full, shadows | Level 2, shadows |
|---|---|---|---|---|---|---|
| Terrain 1M | 7.71 (11.6) | 4.38 (11.6) | 2.19 (9.2) | — | 11.49 (16.8) | 3.90 (9.4) |
| Terrain 3M | 10.97 (14.8) | 8.71 (14.3) | 3.79 (7.2) | 1.94 (6.3) | 20.48 (23.6) | 6.71 (12.8) |
| Terrain 5M | 14.42 (17.4) | 13.88 (16.7) | 4.99 (7.8) | 2.38 (3.8) | 29.78 (32.9) | 10.37 (14.6) |
| Dragon 7.2M | 11.15 (16.2) | 8.17 (12.7) | 3.65 (8.7) | 1.65 (5.0) | 21.39 (25.1) | 7.20 (12.9) |
| Statuette 10M | 21.05 (25.6) | 9.43 (15.4) | 4.51 (11.4) | 2.24 (8.6) | 42.23 (49.2) | 9.16 (15.9) |

**AMD Radeon Pro 5300M (dedicated)**

| Model | Full detail | Level 1 | Level 2 | Level 3 | Full, shadows | Level 2, shadows |
|---|---|---|---|---|---|---|
| Terrain 1M | 1.91 (1.95) | 0.87 (0.98) | 0.47 (0.75) | — | 3.25 (3.34) | 0.95 (0.97) |
| Terrain 3M | 5.34 (5.35) | 2.00 (2.05) | 0.84 (0.93) | 0.42 (1.03) | 8.06 (8.11) | 1.61 (1.66) |
| Terrain 5M | 8.82 (8.84) | 3.74 (3.77) | 1.20 (1.36) | 0.53 (0.77) | 8.92 (13.2) | 2.36 (2.41) |
| Dragon 7.2M | 9.10 (9.11) | 2.78 (2.79) | 0.98 (1.02) | 0.36 (0.37) | 13.90 (13.9) | 1.95 (1.99) |
| Statuette 10M | 16.40 (16.45) | 4.19 (4.23) | 1.18 (1.20) | 0.49 (1.01) | 24.66 (24.7) | 2.81 (2.95) |

- **CPU time** to prepare and submit a frame was 0.1 ms (median) at every level
  on both GPUs, at most 4.4 ms; one draw call per mesh (three with shadows: the
  shadow pass draws the model again). The frame cost is on the GPU.
- **The first frame** of a level includes uploading its geometry: 465 ms of CPU
  time for the Statuette at full detail (Intel), 85 ms for level 1, under 4 ms
  for the others.
- **Levels help most on the dedicated GPU** (Statuette: 16.4 → 4.2 → 1.2 ms).
  On the Intel GPU level 1 helps little on the terrains (5M: 14.4 → 13.9 ms)
  but level 2 cuts it by two thirds; its timings vary more (95th percentiles
  well above the medians).
- **Shadows** double the triangles drawn and roughly double GPU time; they start
  off for models over 1.5 million triangles.
- **Auto** switched one level at a time, in order, while zooming out and back in
  on every model (for example 625K → 156K → 625K → 2.5M on the Statuette);
  framed, it drew a level, not full detail.

## Picking and measuring

What a click or a measurement does: a ray from the view against the full model
(levels are never picked). Median ms per ray, from the rendering runs (10 timed
rays after 2 warm-up rays; Intel GPU runs, the CPU does this work):

| Model | three.js (every triangle) | With the picking index (D64) |
|---|---|---|
| Terrain 1M | 101 | 4.2 |
| Terrain 3M | 304 | 4.7 |
| Terrain 5M | 494 | 5.1 |
| Dragon 7.2M | 752 | 5.1 |
| Statuette 10M | 1,154 | 1.7 |

The index gave the same nearest hit as three.js on every compared ray (unit
tests: 200 rays, indexed and not, front- and double-sided). Building it took
1.6–2.0 s of CPU time for 10 million triangles. The first version built it at
the first click, which then took 2,215 ms on the Statuette; it is now built
after loading, in idle time, about 10 ms at a time: the first click on the
Statuette then took 9.2 ms (median 2.7 ms, at most 17.7 ms over 10 rays; Rust
and frontend checks were running on the same CPU during that run, so these are
upper bounds).

## Loading (`e2e/benchmarks/large-models.spec.ts`)

```bash
AI3D_BENCHMARK_GLBS=OUT/xyzrgb_statuette/model.glb [AI3D_BENCHMARK_DOWNLOAD_MBPS=50] \
  pnpm exec playwright test e2e/benchmarks/large-models.spec.ts --project=desktop --workers=1
```

Through the web app, from opening the page, with the levels already built.
Intel GPU.

| Model | Upload + check | Levels built (in the app) | First view (preview) | Full model shown | JS heap |
|---|---|---|---|---|---|
| Terrain 1M | 5.4 s | 13.2 s | 4.4 s | 6.4 s | 136 MB |
| Terrain 3M | 9.6 s | 31.4 s | 2.2 s | 7.4 s | 266 MB |
| Terrain 5M | 15.3 s | 53.7 s | 2.1 s | 10.5 s | 402 MB |
| Dragon 7.2M | 22.8 s | 69.8 s | 3.2 s | 8.7 s | 424 MB |
| Statuette 10M | 35.5 s | 105.3 s | 3.0 s | 12.7 s | 584 MB |

- The *first view* is the coarsest level, labelled as a preview, while the full
  file downloads; then the full model replaces it.
- **Slow connection (50 Mbit/s, throttled in the browser):** the preview used
  to wait for every level to download. Now only the coarsest level loads before
  the full model, and the finer ones follow it (checked in `e2e/lod.spec.ts`).
  Statuette: preview 18.6 → 3.4 s, full model 28.9 → 21.9 s; Dragon: preview
  14.1 → 5.0 s, full model 23.2 → 16.9 s. The development server compresses
  responses, so these files arrived faster than 50 Mbit/s of raw GLB would
  allow; the comparison between the two versions is what is valid.
- **After loading** (Statuette, the 15 s after the full model appears, longest
  main-thread tasks from the browser's Long Tasks API): first 547 and 390 ms. A
  CPU profile showed the full model's bounding box being recomputed each time
  another level arrived (a third of a second over 10 million triangles, during a
  React render). Each part's box is now kept while its positions are unchanged
  (`lod.test.ts` checks it is measured once): longest task 95 ms afterwards.
  The picking index runs in slices of about 10 ms meanwhile.
- **Still there:** when the full model replaces the preview, the preview's
  WebGL context is released (`forceContextLoss` in React Three Fiber's
  teardown), which took 0.15–0.6 s in the profiles, and creating a context
  took 0.2–0.3 s. These were measured on the development server, where React
  mounts the canvas twice; not changed, as avoiding it would mean drawing the
  preview and the full model in one canvas.

## Screenshots and editing at full detail

Picking, measuring, editing and saving always use the full model; levels are
only drawn (D58). Screenshots render full detail whatever level is drawn.
Measured on the Statuette (Intel GPU), with Auto drawing level 2 (625K
triangles) at the time:

| Screenshot size | Pixels | From the click to the saved PNG | PNG |
|---|---|---|---|
| 2× (the default) | 1312 × 1693 | 270 ms | 0.3 MB |
| 4× | 2624 × 3386 | 518 ms | 1.0 MB |

Both render all 10 million triangles, then the view returns to its level. So
full-detail screenshots are safe at these sizes on this machine (captures are
limited to 8192 px a side and to what the GPU allows; Rust and frontend checks
were running on the same CPU during this run). Selecting, measuring and moving
parts work on the full model through the picking index (2–18 ms per click on
the Statuette); a part whose geometry is edited is drawn in full from then on.

## Memory in the browser

- Statuette loaded: JS heap 335–652 MB depending on the run, of which the full
  model's geometry is 260 MB, in a 1440 × 900 window.
- **Switching versions** (`e2e/viewer-memory.spec.ts`: a textured 82K-triangle
  version and two others, four rounds): the renderer's geometry and texture
  counts return to the same values every round. The JS heap grew 6.6 MB per
  round on the development server, which keeps old components for its tools; on
  a production build (`-c playwright.production.config.ts`) it grew 0.45 MB per
  round, and the test checks that it stays within 5 MB.

## Not measured

- Apple Silicon, Windows and Linux desktops (see
  [HARDWARE_TESTING.md](HARDWARE_TESTING.md)). CI runs on Linux with software
  WebGL and gives no performance numbers.
- This machine at full power (Low Power Mode was on).
- Exports of the 15M and 20M terrains, and smoothing above 10 million triangles.
- Rendering above 10 million triangles in the browser.
- Real slow networks (only the browser's throttling, through a compressing
  development server).
