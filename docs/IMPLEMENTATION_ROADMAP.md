# AI 3D Studio — Implementation Roadmap

Each phase ends with a working application, green checks (lint, types, unit,
integration, e2e, builds) and updated docs. "Done" below means implemented
**and** covered by tests that were executed.

## Phase 0 — Foundation ✅

- Monorepo: pnpm workspace (apps, packages) + uv workspace (backend, local-engine).
- Docs: product spec, architecture, AI pipeline, database schema, roadmap,
  contributing guide, installation, providers, local GPU setup, troubleshooting.
- Tooling: ESLint 10 + typescript-eslint, Prettier, TypeScript (strict), ruff,
  mypy (strict), Vitest + RTL, Pytest, Playwright, rustfmt + clippy.
- Generated TS API types from the backend OpenAPI schema (`pnpm gen:api`, CI check).
- Docker images (backend, web) + Compose; GitHub Actions CI (6 jobs).
- Desktop: Tauri 2 shell with Vite entry, strict CSP, minimal capabilities.

## Phase 1 — Shell, backend, database, projects, uploads ✅

- FastAPI app factory, structured logging (JSON/console), request IDs, uniform
  errors, body-size limit, CORS for web + desktop origins.
- SQLAlchemy 2 models + Alembic migration for all 15 tables; SQLite (WAL) and
  PostgreSQL verified.
- Local file storage (atomic writes, traversal-safe keys).
- Projects: create, list (search, status filter, pagination), get, update,
  archive/restore, delete (rows + files).
- Images: multi-file upload with per-file results, content-based validation
  (JPEG/PNG/WebP), size/pixel/count limits, SHA-256 dedupe, original bytes
  preserved, EXIF subset (GPS excluded), orientation-aware thumbnails,
  quality analysis (sharpness, exposure, clipping, contrast, alpha coverage).
- Capture assistant: set-level checks (count, blur, lighting, resolution,
  focal length, reflective/low-texture hints), shooting guide; overlap
  honestly `not_assessed`.
- Hardware/dependency detection (`/api/v1/devices/local`) with conservative
  capability verdicts.
- UI: landing, projects dashboard, new project, project workspace (upload,
  queue with progress, grid, details, capture check), settings (backend +
  machine), about/credits; responsive (phone top bar), keyboard accessible,
  reduced motion.

## Phase 2 — Provider interface, jobs and first real integration ✅ (real providers untested)

- **Provider protocol** (`ai3d/providers/base.py`): capability discovery,
  per-quality-mode option translation, cost estimates, normalised errors,
  submit/poll/cancel/fetch. The pipeline never imports a concrete adapter.
- **Tripo v3** and **Meshy** adapters, tested against fake servers that follow
  the documented wire formats (upload, task, progress, failure, expired links,
  rate limits). Results are downloaded as soon as a task succeeds.
- **Mock provider** (test-only): registered only with `AI3D_ENVIRONMENT=test`,
  returns a labelled placeholder sphere.
- **Persistent job runner** (`ai3d/workers/`): compare-and-set claiming, leases
  with heartbeats, cooperative cancel, restart recovery that resumes the
  existing provider task (no double charge), retries linked by `parent_job_id`.
- **Job API**: plan preview, create (Idempotency-Key), list, get, cancel, retry,
  consent, event timeline, and a Server-Sent Events stream with
  `Last-Event-ID` resume (`no-transform`, so proxies don't buffer it); the UI
  falls back to polling.
- **Consent flow**: `waiting_for_consent`; nothing is uploaded until the
  project allows cloud use *and* the job is approved.
- **Provider preprocessing**: upright, sRGB, resized copies with all metadata
  stripped; originals untouched.
- **Provider keys**: environment, or stored encrypted (`AI3D_SECRET_KEY` or a
  generated `secret.key`); only a 4-character hint is ever returned; key check
  via the provider's balance endpoint.
- **Mesh validation** (trimesh): re-opens the stored file, checks finite
  geometry, counts, bounds, components, watertightness; models and versions
  recorded with an "AI-generated: unseen surfaces invented" label.
- **UI**: generation panel (photo/views, quality, processing mode, provider,
  options preview with estimated cost and warnings, consent), live job cards
  (progress, timeline, cancel, retry, permission prompt), model list (preview,
  GLB download, delete), provider keys in Settings.

Exit criteria: mock pipeline e2e ✅; adapters unit-tested ✅; **real generation
with Tripo or Meshy not yet verified** — it needs a paid API key.

Verified on 2026-11-03 (all executed): 161 backend tests; 45 frontend unit
tests; 12 Playwright e2e tests (6 desktop + 6 mobile); ruff, mypy (strict),
ESLint, Prettier, tsc; generated API types up to date; web and desktop builds;
cargo fmt, clippy and test. CI on GitHub has not been run.

## Phase 3 — 3D workspace ✅

- **`packages/viewer`** (React Three Fiber + drei): GLB loading (Draco and
  meshopt decoders are wired in and bundled, but no compressed file has been
  tested yet), orbit/pan/zoom, camera presets
  (fit, front, back, left, right, top, bottom), image-based lighting generated
  locally, key light with shadows, grid, axes, bounding box, Material / Solid /
  Wireframe display, click-to-select with a selection box. Scene facts are
  computed from the real geometry: parts, vertices, triangles, materials,
  textures and exact world-space dimensions (shown with the file's units and,
  for AI output, a "no real-world scale" note).
- **`packages/editor`**: undo/redo history (limit 200, folded change list),
  commands for move/rotate/scale (gizmo or numeric fields) and material colour,
  roughness, metalness and opacity (live preview, one undo step per change),
  GLB export with the model's own materials.
- **Versions**: `POST /models/{id}/versions` stores an edited GLB as a new
  `edited` version (parent kept, provenance inherited, file re-opened and
  checked, change list in the notes); `GET /models/{id}/versions`;
  `PATCH /models/{id}` renames or switches the current version (nothing is
  deleted). Version panel with view, make current, download.
- **Workspace screen** (`/projects/{id}/models/{id}`, web and desktop):
  hierarchy (with view-only hide/show), inspector (model facts, selection,
  transform, materials, quality report), keyboard shortcuts, unsaved-edit
  guards, WebGL-unavailable fallback, responsive phone layout. three.js loads
  only when a model is opened (separate chunk).

Not in Phase 3 (and not shown as if it were): vertex-level mesh editing,
texture settings (filtering, replacing images), KTX2/Basis compressed
textures, editing glTF animations (they are preserved on save).

Verified on 2026-11-03 (all executed): 170 backend tests; 75 frontend unit
tests; 16 Playwright e2e tests (8 desktop + 8 mobile, real WebGL in local
Chrome); the workspace also checked by hand in the browser (select, gizmo
drag, save, wireframe, phone layout); ruff, mypy (strict), ESLint, Prettier,
tsc; web and desktop builds; cargo fmt, clippy and test. CI has not been run.

## Phase 4 — Multi-image photogrammetry ✅ (dense/CUDA path untested)

- **`ai3d_engine.reconstruction`**: COLMAP 4 runner (allow-listed subcommands,
  arguments built in code, no shell, live progress from the log, cancellation
  and timeouts that always stop the process, version check ≥ 4.0); text-model
  reader; upright alignment from camera axes; pipeline features → matching →
  mapping → Open3D point filtering → Delaunay surface (sparse without CUDA,
  dense with it) → texture (`mesh_texturer`, CPU).
- **Job integration**: `photogrammetry` jobs run in the backend's job runner
  on this computer (one at a time), with stage progress, cancel, restart after
  an interruption, meaningful failures (no starting pair, too few photos
  placed, unused photos, separate groups, weak photos).
- **Models**: saved as `reconstructed` versions with `measured` provenance,
  the filtered point cloud as a second file, reconstruction facts in the
  quality report, and "scale unknown" in the notes and inspector.
- **Routing**: Local processing or the *Photogrammetry* method sends a capture
  set here; Automatic mode prefers it for 20+ photos when COLMAP is available;
  a single photo is never sent to it. No consent step: nothing is uploaded.
- **Capture check**: *Measure overlap* (OpenCV SIFT + RANSAC) reports isolated
  photos and disconnected groups; cached per photo set.
- **UI**: method choice (Photogrammetry / AI multi-view), photo selection,
  COLMAP status in Settings, stage labels.
- **Fix found on the way**: the shared file check counted UV-seam vertex splits
  as separate pieces and holes; topology is now judged on the merged shape
  (affects provider output too).

Verified on this Intel Mac with COLMAP 4.2.1 (CPU, conda-forge) and Open3D
0.19.0: 30 synthetic photos → all 30 placed, ≈ 7,500 points, 0.31 px mean
reprojection error, textured 11k-triangle surface standing upright, in ≈ 50 s.
Not verified: real camera photos, dense reconstruction (needs CUDA), Windows
and Linux.

Checks run on 2026-11-03 with `AI3D_COLMAP_PATH` set: 198 Python tests
(including real COLMAP runs: success, non-overlapping photos, cancellation);
78 frontend unit tests; 17 Playwright e2e tests plus 1 skipped by design (the
full reconstruction runs on desktop only); ruff, mypy (strict), ESLint,
Prettier, tsc, web and desktop builds, cargo fmt/clippy/test. One frontend
unit run showed 4 failures that did not reproduce in 5 later runs (including
under full CPU load); cause not identified. CI has not been run.

## Phase 5 — Geometry & texture processing, quality reports, export ✅

- **Processing chain** (`processing/geometry.py`, trimesh + fast-simplification):
  clean-up (degenerate/duplicate triangles), loose-fragment removal,
  consistent normals, hole filling (untextured meshes only), texture size
  limits, decimation (untextured only), ground/table removal (RANSAC plane +
  anything under it + largest piece), real-world scale from one measurement.
  Topology is computed on a seam-merged copy so textured meshes keep their UVs.
- **Quality profiles**: Fast (clean-up, fragments, 2048 px textures), High
  (+ normals, holes, 4096 px), Professional (+ 300k-triangle budget, 8192 px,
  texture baking with Blender; retopology listed as unavailable).
- **Enhance workflow**: `POST /models/{id}/enhance(/plan)` → background job on
  this computer → new `enhanced` version (source kept, provenance inherited,
  steps recorded); retry and restart supported; no new version when nothing
  changed. Known scale is inherited by later edits and enhancements.
- **Quality report v2**: boundary and non-manifold edges, duplicate faces,
  winding consistency, volume (closed meshes), UV range, texture sizes; new
  Quality tab with geometry / surface / size / reconstruction / steps.
- **Export center**: GLB, glTF (zip), OBJ+MTL (zip), STL and 3MF (millimetres),
  USDZ (Pixar USD, optional `usdz` extra) — each re-opened with an independent
  reader and compared (triangles, size, texture); mismatches are never offered.
  FBX via Blender when installed (see *Blender* below), otherwise unavailable.
- **Sizes**: shown in metres once a measurement sets the scale, otherwise in
  the model's own units.

Measured on the synthetic photogrammetry scans: after ground removal the box's
proportions were 7–25% off (coarse sparse surfaces), so the measurement fixes
one dimension exactly and the others carry that error. Not verified: real
photos.

**Blender follow-up (2026-11-04, Blender 4.5.15 LTS):** FBX export (textures
embedded, metres, Y-up; re-opened in a separate Blender run and compared) and
texture baking for textured models over the Professional budget (reduced copy,
new UVs, base colour baked with Cycles on the CPU; median colour error 1/255
on the test sphere). Two exporter problems were found and fixed: tinted
textures were dropped, and only the first of several textures was embedded.
Retopology stays unavailable (QuadriFlow moved open surfaces; GLB holds only
triangles). Only base colour is baked.

Checks run on 2026-11-03: 222 Python tests passed in the full run (the 3
real-COLMAP tests were skipped there for an unidentified reason and passed in
a separate run with `AI3D_COLMAP_PATH`); 82 frontend unit tests; 19 Playwright
e2e tests plus 1 skipped by design; ruff, mypy (strict), ESLint, Prettier, tsc,
web and desktop builds, cargo clippy/test. The Enhance and Export tabs were
also checked by hand on a photogrammetry model. CI has not been run.

## Phase 6 — Desktop app & local agent ✅ (release bundling of the engine in Phase 7)

- **Private local engine (sidecar)**: the Tauri app starts `ai3d-server`
  itself on `127.0.0.1`, on a random free port, with a fresh 256-bit token per
  launch and the app's own data folder (`~/Library/Application
  Support/org.ai3dstudio.desktop` on macOS). It waits for `/api/v1/health`,
  restarts a crashed engine (3 times, with back-off), and on quit sends
  SIGTERM so running jobs are handed back for recovery. Output goes to
  `logs/backend.log` in the data folder (truncated above 5 MB).
- **Per-launch token** (`AI3D_API_TOKEN`): every `/api/*` call except
  `/api/v1/health` needs `Authorization: Bearer …` (or `?access_token=` for
  `<img>`, downloads and live progress). Other programs and web pages on the
  machine reach the port but get 401. The token is never logged.
- **Watchdog** (`AI3D_PARENT_PID`): the engine exits by itself when the app
  disappears without a clean quit (crash, force quit, `tauri dev` rebuilds).
- **Restricted Rust commands**: app info, engine status, restart engine, save
  a backend download, open the data folder. Saving accepts only model-version,
  export and image download paths; the destination comes from the native Save
  dialog, and the file name is reduced to a bare, safe name. The window has no
  shell, file-system, HTTP, dialog or opener permissions of its own.
- **Desktop UI**: start-up screen while the engine starts; on failure the
  reason, the last log lines and *Try again*. Downloads and exports open the
  native Save dialog; Settings has *Open* for the data folder; About shows the
  engine's port and log path.
- **Tests**: Rust unit tests (token, ports, log tail, download allow-list,
  file names) and an integration test that starts the real engine, checks the
  token (401 without, 200 with), the data folder, the log, saving a download,
  restart after `SIGKILL` and a clean stop. Backend token/watchdog tests.
  A Playwright suite (`pnpm test:e2e:desktop`) drives the desktop UI against a
  token-protected engine: create, upload, thumbnail, 3D model and download all
  work through the token, with no 401s.

Verified by hand in `pnpm dev:desktop`: engine on a random port with the app's
data folder, migrations, token-authenticated requests from the window, and the
watchdog (engine exited about 1 s after the app was force-killed).

Not done or not verified: release builds do not contain the engine yet (a
`tauri build` app shows "The local engine is not included in this build"
unless `AI3D_DESKTOP_BACKEND` points to one; bundling is Phase 7). The Tauri
window itself is not automated (Tauri's WebDriver does not support macOS);
the native Save dialog and *Open data folder* were not clicked through by hand.
The normal Quit path is covered only by the integration test's `stop()`.
No local AI provider: this machine has no suitable GPU, so none was added.

Checks run on 2026-11-04 (with COLMAP 4.2.1 and Blender 4.5.15 installed):
250 Python tests (none skipped); 85 frontend unit tests; 19 web e2e tests plus
1 skipped by design (FBX exported and verified through the UI); 2 desktop UI
e2e tests; 11 Rust unit tests and 1 engine integration test; ruff, mypy
(strict), ESLint, Prettier, tsc, web and desktop builds, cargo fmt/clippy.
CI has not been run.

## Phase 7 — Testing, documentation & release packaging ✅ (Windows/Linux builds and signing unverified)

- **Desktop release builds bundle the engine** (`pnpm build:desktop-app`):
  - **What's inside:** a relocatable CPython 3.11 with the backend installed
    from `uv.lock` (hash-checked), byte-compiled, with a licence list and a
    smoke test (D24).
  - **How the app runs it:** `python -I -m ai3d.cli` from its data folder, so
    `<data folder>/.env` configures it.
  - **Finding tools:** COLMAP is also looked for in Homebrew's folders and in
    the self-contained install from LOCAL_GPU_SETUP, because apps started from
    Finder don't see the shell's PATH.
  - **Verified on macOS Intel:**
    - The `.app` (670 MB) started its bundled engine and listed projects
      through the token.
    - A normal Quit shut the engine down cleanly. That was the Phase 6 gap;
      relaunching also worked.
    - The `.dmg` (184 MB) passes `hdiutil verify`, and the engine imports from
      the mounted image.
- **Restart and recovery, end to end** (`test_restart_recovery.py`):
  - The real server process is killed with SIGKILL in the middle of a
    generation, then restarted on the same data folder.
  - The project, photo, thumbnail, finished model and job history are all
    there.
  - The interrupted job resumes the same provider task. Its photo was sent
    once, before the restart, and never again.
- **Job workflows in the browser** (`pnpm test:e2e:workflows`):
  - A provider error is shown, and retrying succeeds.
  - A running job is cancelled, then retried.
  - The history survives a reload.
  - These use two test-only settings of the mock provider:
    `AI3D_MOCK_POLLS_UNTIL_DONE` and `AI3D_MOCK_FAIL_FIRST_TASK`.
- **S3-compatible storage** (`s3` extra, D25):
  - Tested against SeaweedFS: the storage contract tests (24) and the whole
    backend suite with every file stored in a bucket.
- **Optional error reporting** (`errors` extra, `AI3D_SENTRY_DSN`, D26):
  - Errors only. No request data, local variables, host name or user is sent.
  - Tokens, keys and the home path are scrubbed.
  - Tested with a captured event.
- **Release workflow** (`.github/workflows/release.yml`):
  - Desktop builds for macOS (Apple Silicon and Intel), Linux and Windows go
    to a draft GitHub release, with licence lists.
  - Container images go to GHCR.
  - Optional macOS signing and notarization from secrets.
- **Docs:**
  - New: [TESTING.md](TESTING.md) (every workflow mapped to its tests),
    [RELEASING.md](RELEASING.md), [ERROR_REPORTING.md](ERROR_REPORTING.md) and
    `CHANGELOG.md`.
  - Updated: architecture decisions D24–D27, installation, troubleshooting,
    README and `.env.example`.
- **Container images** (verified on Docker 29.8):
  - Measured sizes: backend 259 MB compressed (1.19 GB on disk), with
    PostgreSQL and USDZ support; web 94 MB.
  - Both images run as non-root, with a read-only root filesystem and no
    Linux capabilities.
  - Compose smoke test passed: health, upload and thumbnail through the web
    proxy; data survived `down`/`up`. The PostgreSQL profile passed too.
  - The opt-in `backend-colmap` target (COLMAP 4.2.1, CPU, 414 MB compressed)
    ran a real 10-photo reconstruction inside a read-only container.
- **CI:**
  - Installs COLMAP and the optional extras, so the real reconstruction, USDZ,
    S3 and error-reporting tests run.
  - New S3 job (SeaweedFS), the two new browser suites, and a compose smoke
    test with a persistence check.
- **No Celery/Redis** (D27): nothing needs distributed workers yet.

Checks run on 2026-11-04 (COLMAP 4.2.1 and Blender 4.5.15 installed):

- **Python:** 262 tests passed. The 7 skips are the S3 variants, which were
  run separately against SeaweedFS: 25 storage tests, and the whole backend
  suite on S3 (266 passed).
- **Frontend:** 85 unit tests.
- **Browser tests:** 19 web e2e tests plus 1 skipped by design; 2 desktop UI
  tests; 2 job-workflow tests.
- **Rust:** 12 unit tests and 1 engine integration test.
- **Static checks:** ruff, mypy (strict, including the bundle script),
  ESLint, Prettier, tsc, web and desktop builds, and cargo fmt/clippy.
- **Packaging:** `pnpm bundle:engine` with its smoke test, and the compose
  smoke tests.

GitHub Actions has not been run. The hand-checked `.app`/`.dmg` was built
before the last backend changes (S3, error reporting, test knobs); the engine
bundle was rebuilt and smoke-tested after them, but the app was not rebuilt.

## Phase 8 — Local single-image AI engine ✅ (CPU on macOS Intel verified)

- **Engine:**
  - TripoSR (MIT code and weights), installed with `ai3d-local-ai install`.
  - Lives in its own folder and Python environment (D28), with pinned sources
    and SHA-256-checked downloads.
  - A real test generation must pass before the engine is marked installed.
- **Run-time safety** (D29):
  - fixed runner; offline model lookups;
  - tensors-only checkpoint loading;
  - no API keys in the engine's environment.
- **Pipeline:**
  - metadata-free working copy of the photo;
  - U²-Net background removal;
  - TripoSR shape and colours;
  - marching cubes on a 160³ / 256³ / 320³ grid (Fast / High / Professional);
  - glTF axes and linear vertex colours (both fixes found by looking at
    renders of real output);
  - the usual validation, quality report and AI provenance.
- **Routing:**
  - **Local** and **Automatic** modes keep a single photo on the computer when
    the engine is installed. Without it, Local mode explains how to install
    it.
  - Cloud mode is unchanged.
  - Local AI and COLMAP share one heavy-job slot (D30).
- **UI:** *Local AI (TripoSR)* appears in the provider list, marked
  *(not installed)* until it is installed. There is no upload consent step:
  nothing is uploaded.
- **Tests:**
  - Engine wrapper (10): status, request validation, minimal environment,
    progress, failure, cancel, timeout, download checksums, unsafe archives.
  - Routing and job (5), with a stand-in for the engine call only.
  - **Real runs on a real phone photo** (TripoSR's own MIT example):
    - API test: 82 s, 97,944 triangles, upright, coloured, quality report not
      failing.
    - Browser test (`e2e/local-ai.spec.ts`): Local processing → Done →
      opened in the 3D workspace, 38,028 triangles at Fast.

Checks run on 2026-11-04 (TripoSR engine, COLMAP and Blender installed):
- **Python:** 278 tests passed. The 8 skips are the S3 variants and the
  real-engine test. The real-engine tests passed separately with
  `AI3D_LOCAL_AI_DIR`: the API test in 82 s, and 16 local AI tests in total.
- **Frontend:** 87 unit tests.
- **Browser tests:**
  - web: 19 passed;
  - desktop UI: 2;
  - job workflows: 2;
  - local AI with the real engine: 1.
- **Rust:** 13 tests.
- **Static checks:** ruff, mypy (strict), ESLint, Prettier, tsc and builds.
- **Hardware detection** now reports local AI as possible on any machine with
  8 GB of RAM (measured peak: 3.7 GB).

**Not done or not verified:**
- NVIDIA GPU (CUDA) path and Windows/Linux/Apple Silicon are untested.
- No installing from inside the app yet: the engine needs `uv` and a
  terminal.
- Not included in desktop release builds.
- No preview image for local models.
- ~~Colour fidelity is modest~~: the muted colours were a bug. Models were
  mirrored, inside out and coloured from mirrored positions. Found and fixed
  in Phase 11.
- First run after installing is slow (200 s for the installer's test while
  macOS checked the new libraries).

## Phase 9 — Real-world verification ✅ (with one major finding)

Real inputs were run through the whole application: the real TripoSR model,
four other single images (including a real photo with a cluttered background),
a real 67-photo capture (AliceVision's Buddha dataset, CC BY 4.0, 2736×1540
photos) and Draco files made by an independent encoder (Blender). Bugs found
by this, all fixed and covered by tests:

- **STL and 3MF lay on their back in slicers.** They were Y-up; they are now
  millimetres with Z up (D31).
- **Colours were lost or wrong, silently:**
  - 3MF dropped them; it now writes a 3MF colour group.
  - USDZ and FBX dropped them; they now use displayColor and Blender's
    sRGB colours respectively.
  - OBJ wrote them too dark (linear values); they are now sRGB.
  - Export validation now checks colours like textures (D32).
- **A Blender-made, Draco-compressed GLB** was rejected: there was no Draco
  decoder (now DracoPy, D34). Its float RGB colours next to a material also
  crashed re-export; colours are now normalised on load (D33). The viewer
  decodes Draco too (new e2e test).
- **The capture check reported 8 disconnected groups and 25 unusable photos**
  on a capture where COLMAP placed all 67. Upload order is not capture order.
  Every pair is now compared up to 120 photos (2,211 pairs in 20–35 s);
  larger sets add image retrieval with a small visual vocabulary (885 pairs
  gave the same answer on the shuffled real set).
- **The quality report passed a real reconstruction** whose bounding box was
  18× larger than its surface. It now warns about stray surfaces (bounding box
  more than 4× the central 95% of the surface) and shattered surfaces (many
  pieces, none holding half).
- **Local AI output had no normals,** which caused faceted, banded shading;
  smooth normals are now written.

**Major finding:** on real photos, photogrammetry without an NVIDIA GPU does
not produce a usable surface.
- COLMAP placed every camera: 67 of 67, 0.45 px reprojection error, 215 s on
  the CPU.
- The surface built from sparse points (18k points) is spiky: it reaches into
  the background, and the statue is not recognisable.
- Ground removal then kept the wrong piece. A guard against removing "too
  much" was tried and reverted, because synthetic tests show a table can
  legitimately be 98% of a scan.
- The report now warns, but the real fix is a dense step that runs on a CPU
  (for example OpenMVS, AGPL-3.0, run as a separate program). That needs a
  decision.

**Verified:** every export of the real TripoSR model, with colours checked in
Blender for GLB, OBJ, FBX and USDZ. Local AI on four varied inputs: all
shapes were plausible, 40–56 s each; colours come out duller than the photos,
which is TripoSR's own output.

Checks run on 2026-11-04 (COLMAP, Blender and the TripoSR engine installed):
- **Python:** 297 tests passed. The 9 skips need settings, and these ran
  separately and passed: the real capture in 4.9 min, the local AI tests
  (16, with the real engine), and S3 in an earlier run.
- **Frontend:** 87 unit tests.
- **Browser tests:**
  - web: 20, including the new Draco test;
  - desktop UI: 2;
  - job workflows: 2.
- **Rust:** 13 tests.
- **Static checks:** ruff, mypy (strict), ESLint, Prettier, tsc and builds.

**Not verified:**
- The 3MF colour group in a real slicer.
- USDZ colours in Apple Quick Look.
- Tripo/Meshy, which still need a key.

## Phase 9b — Dense photogrammetry on the CPU (OpenMVS) ✅

Fixes Phase 9's major finding.
- **OpenMVS 2.4.0** (AGPL-3.0, conda-forge CPU build) is installed separately
  and run as allow-listed programs (D35). It does dense depth maps, fusion,
  meshing and texturing.
- **Quality modes:** Fast uses depth maps at 1/4 of the photo size and keeps
  half the triangles; High and Professional use 1/2 and keep them all.
- **Engine choice:** OpenMVS is used when installed; otherwise COLMAP's CUDA
  dense step (still untested); otherwise the sparse surface, which the plan
  and the quality report now describe honestly.

Two more real-data bugs were fixed on the way:
- **The model came out lying on its side.** The capture had portrait and
  top-down shots, so the "up" rule from Phase 4 was 92° off. Up now falls
  back to the direction of the cameras from the object when their x axes are
  not coplanar (D36).
- **Ground removal missed the board under the statue.** It was a few % of a
  dense scan. The plane is now searched for at the bottom of the model, with
  extra checks so curved bottoms are kept (D37).

**Result on the real 67-photo capture,** through the application (upload →
overlap check → reconstruction → Enhance → exports):
- Every camera placed.
- Dense, textured surface: about 400k triangles in about 7 minutes on a
  6-core Intel Mac (COLMAP about 2.5 min, OpenMVS about 4.5).
- Upright; ground removal took off the board (11% of the triangles) and kept
  the whole statue, with face, ears and curls.
- The quality report is clean, and every export was re-opened and verified.

Checks run on 2026-11-04:
- **Python:** 305 tests passed. The 9 skips need settings; these ran
  separately with them and passed:
  - the real capture with OpenMVS (9.6 min);
  - the 24 photogrammetry tests with OpenMVS;
  - the OpenMVS runner tests (6) are part of the 305.
- **Frontend:** 87 unit tests.
- **Browser tests:** web 20, desktop UI 2, job workflows 2.
- **Rust:** 13 tests.
- **Static checks:** ruff, mypy, ESLint, Prettier, tsc and builds.

**Not verified:**
- Other real captures (only one dataset so far).
- OpenMVS on Apple Silicon, Windows or Linux.
- COLMAP's CUDA dense path.
- RefineMesh, which is not used.

## Phase 10 — Hybrid processing and resources ✅ (cloud side tested only with the mock provider)

**Resource checks,** measured on this machine (6-core Intel Mac):
- Local AI (TripoSR) peaks at 3.7 GB of memory. Photogrammetry with OpenMVS
  peaked at 1.9 GB (texturing). A 540 MB, 67-photo capture used about 1 GB of
  workspace, so the check asks for 3× the photos (at least 2 GB).
- **At planning time** (D38):
  - Too little free disk blocks the job (`not_enough_disk`).
  - A computer with under 8 GB of installed memory is too small for local AI
    (`machine_too_small`): Local mode explains why; Automatic mode uses the
    cloud instead, with the usual consent, and says so.
  - Too little *free* memory only warns: closing programs is enough.
- **Just before running:** the disk is checked again once the job has its
  turn, so a disk that filled up while it waited fails cleanly, before the
  engine starts.

**One heavy job at a time** now covers Blender texture baking as well as
COLMAP/OpenMVS and local AI. Only the Blender part of Enhance waits; the
quick steps never do (D30).

**Offered fallback** (D39). A failed generation lists other engines that could
run it, with a reason for each:
- A failed local job offers the configured cloud services. Choosing one shows
  what is uploaded and to whom, and nothing is sent until the person confirms.
  A local-only project still asks for its own permission.
- A failed cloud job offers the local engines ("nothing is uploaded or
  charged").
- A job in **Local** mode is never offered the cloud.
- Nothing switches by itself: the offer is a new job (a retry with another
  provider), and the backend rejects a provider that was not offered.

Checks run on 2026-11-04:
- **Python:** 311 tests passed, plus the COLMAP tests run separately with
  `AI3D_COLMAP_PATH` set (11 passed). New tests are in `test_resources.py`,
  `test_local_ai.py` (fallback) and `test_processing.py` (baking waits).
- **Frontend:** 90 unit tests, including the fallback buttons in
  `job-list.test.tsx`.
- **Browser tests:** web 19 + photogrammetry 1 (with COLMAP), desktop UI 2,
  job workflows 2.
- **Rust:** 13 tests.
- **Static checks:** ruff, mypy, ESLint, Prettier, tsc and builds.

**Not verified:**
- The cloud side of the fallback against real Tripo/Meshy (no key yet).
- The memory threshold on a real 8 GB machine; the numbers come from one
  computer.
- Model-size limits for the viewer (moved to Phase 12, large models).

## Phase 11 — Editing ✅ (with a major fix to Phase 8)

New edits in the 3D workspace. Each is one undoable step, saved like any
other edit as a new version that the server re-opens and checks.
- **Parts:** delete (button or the Delete key; undo puts the part back in
  place), mirror along X/Y/Z, and move the pivot to the part's centre without
  moving anything on screen.
- **Whole model:** place on the ground; centre on the origin (standing on the
  ground); mirror.
- **Cut with a plane** (new *Cut* tab):
  - choose the axis, the position and the side to keep; the 3D view shows
    what will be kept;
  - closed outlines are filled with a flat cap (holes included), so a closed
    model stays closed for 3D printing. The cap takes the colours of the cut
    edge;
  - open surfaces are left open, and the result says so.
- **Textures:** replace or add a material's base-colour texture (PNG or
  JPEG, up to 8192 px), only when its parts have texture coordinates. A
  *UV layout* view draws the texture coordinates over the texture.

**Decisions** (D40–D42):
- The cut runs in the browser, like the other edits; the server checks the
  saved file. Capping uses three.js' built-in Earcut, so no new dependency.
- Mirroring is stored as a negative scale. The STL and 3MF exports were
  checked to bake it with outward-facing triangles.
- The cap's triangles follow the direction of the cut outline, so they always
  agree with their neighbours.

**Major finding: local AI models were mirrored and inside out (fixed).**
Testing the cut on a real TripoSR model showed inconsistent winding. The cause
was in Phase 8: the PyMCubes stand-in for `torchmcubes` returned coordinates
in the opposite axis order to what TripoSR expects (D29). As a result:
- every local AI model was a mirror image, with its triangles facing inwards;
- the colours were looked up at mirrored positions. The plush's orange feet
  and white belly were missing; the "muted colours" noted in Phase 8 came
  from this.

The fixes:
- The stand-in now matches `torchmcubes`, and the axis conversion was
  re-derived.
- On the same photo the model now has outward faces, orange feet, a white
  belly and its eyes on the front.
- The real-engine test now checks exactly these things.
- The quality report gained an **inside-out** check, which Enhance (High or
  Professional) fixes. Phase 9's checks had looked at size, upright
  orientation and file validity, and missed this.

Checks run on 2026-11-04 (COLMAP configured):
- **Python:** 317 tests passed. The real TripoSR test was run separately with
  `AI3D_LOCAL_AI_DIR` and passed (85 s), with the new checks for outward
  faces, orange feet and eyes on the front.
- **Frontend:** 104 unit tests, including 14 new editor tests
  (`structure.test.ts`).
- **Browser tests:**
  - web: 21, including `e2e/editing.spec.ts` and photogrammetry with COLMAP;
  - desktop UI: 2;
  - job workflows: 2.
- **By hand in the browser:** a cut on a real TripoSR model, saved and
  checked by the server.
- **Rust:** 13 tests.
- **Static checks:** ruff, mypy, ESLint, Prettier, tsc and builds.

**Not done or not verified:**
- Merge and split parts; vertex-level editing.
- Cutting skinned, instanced or shape-key parts: they are skipped and named.
- Cutting a single part only: the cut always applies to the whole model.
- Texture adjustments (brightness, tiling); maps other than base colour.
- Earlier local AI versions stay as they were (versions are never changed);
  regenerate them to get the fix.

## Phase 12 — Viewer ✅ (with two fixes found on a large model)

View-only tools in the 3D workspace. None of them changes the model.
- **Display modes:** Normals (surface direction as colour) and UV (a checker
  texture that shows stretching and seams); parts without texture
  coordinates show solid.
- **Lighting presets:** Studio, Soft and Flat (no key light, for judging
  colours).
- **Section view:** a clipping plane across X, Y or Z with a position slider
  and a flip button.
- **Measuring** (button or *M*): click two points on the surface for the
  distance and its X/Y/Z parts. The units follow the model: real units after
  scaling in Enhance, otherwise the model's own units, with a reminder that AI
  proportions are not measurements.
- **Screenshot:** a PNG of the current view. In the browser it downloads; in
  the desktop app it opens the native Save dialog through a new Rust command
  that accepts only PNG data (D44).
- **Compare versions:** the Versions tab shows another version side by side.
  The cameras move together, and a table lists the stored facts of both, with
  the differences marked.
- **Large models:** shadows start off above 1.5 million triangles. Exact
  bounds are computed per vertex only up to 500,000 vertices (D45).

**Measured on a 3.1-million-triangle model** (56 MB): it opens in 3–6 s in the
browser test, and switching a display mode and selecting a part takes 0.9 s
(2.8 s before the bounds limit).

**Fixes found on the way:**
- **Web uploads over 10 MB hung.** Next.js cut proxied request bodies at
  10 MB, which affected large photos and models in the web app (not the
  desktop app, which talks to its engine directly). The limit is now 520 MB,
  above the backend's own limits (D46).
- **Checking very large models was slow.** Validating a 6-million-triangle
  model took 57 s, 38 s of it in NumPy's row-wise `unique`. One integer key
  per row brought it to 21 s; a 3-million-triangle model takes 9 s (D47).

Checks run on 2026-11-04 (COLMAP configured):
- **Python:** 318 tests passed.
- **Frontend:** 106 unit tests.
- **Browser tests:**
  - web: 23, including `e2e/viewer.spec.ts` (2) and the large model;
  - desktop UI: 2;
  - job workflows: 2.
- **Rust:** 14 tests, including PNG-only saving.
- **Static checks:** ruff, mypy, ESLint, Prettier, tsc, cargo fmt, clippy
  and builds.

**Not done or not verified:**
- Level of detail or progressive loading. Models above roughly 5 million
  triangles have not been tried in the browser.
- Problems from the quality report are still not highlighted on the model.

## Roadmap after the capability audit (2026-11-04)

Based on [CAPABILITY_AUDIT.md](CAPABILITY_AUDIT.md). The goal is a genuinely
capable application; publication comes later. Each phase keeps the app
working and adds tests that use real tools and real data.

**Phase 8 — Local single-image AI engine ✅ (verified on macOS Intel CPU; see below).**
- **The engine:**
  - TripoSR (MIT code and weights) in its own isolated Python environment.
  - Installed on request, showing the licence; weights are downloaded with
    checksums, never bundled.
  - Run through the restricted engine runner.
  - Works on CPU; uses CUDA when present.
- **Pipeline:** background removal, then generation, a GLB, and the existing
  validation and quality report.
- **Routing:** Local and Automatic modes send a single photo to it.
- **Done when:** this Intel Mac turns real photos into models, end to end, in
  the web and desktop apps.

**Phase 9 — Real-world verification ✅ (see above).**
- Real photo captures for photogrammetry (our own photographs, or public
  datasets with compatible licences).
- Real-output checks of the quality report and every export.
- Draco decoding on the backend.
- A Tripo/Meshy smoke test as soon as a key is available.

**Phase 10 — Hybrid processing and resources ✅ (see above).**
- Resource checks before heavy local jobs (RAM and disk; model size moved to Phase 12).
- A single slot for heavy local work.
- An offered, consent-gated fallback between local and cloud when a route
  fails or the machine is too small.

**Phase 11 — Editing ✅ (see above).**
- Delete parts; pivot to centre; place on ground; centre; mirror.
- Cut by a plane, with filled caps (done in the browser, validated by the
  server like every version; D40).
- Replace a texture; a UV layout view.

**Phase 12 — Viewer ✅ (see above).**
- Measuring tool; lighting presets.
- Normals and UV-checker views; section plane; screenshots.
- Version comparison.
- Large-model performance (3 million triangles measured).

**Phase 13 — Geometry.**
- Retopology with a measured fidelity check (QuadriFlow or Instant Meshes).
- Smoothing and remeshing.
- Normal and ambient-occlusion map baking.

**Phase 14 — AI enhancement.**
- Local texture super-resolution (permissive licences only).
- Optional provider refine/retexture with consent.

**Phase 15 — Projects.**
- Import existing 3D files (GLB/OBJ/STL/PLY) as versions.
- Project backup and export.

**Later, needs hardware:**
- TRELLIS (NVIDIA ≥16 GB) as the high-quality local engine.
- The dense COLMAP path.
- Windows, Linux and Apple Silicon builds.

Not planned: billing or subscriptions, public release work.

## Known gaps carried forward

| Gap | Status |
|---|---|
| Tripo/Meshy never run against the real services | Needs a paid key; smoke test whenever one is available |
| GitHub Actions (CI and release) have never run; Windows and Linux desktop builds, and the bundle script on those systems, are untested | First push to GitHub |
| Desktop builds are unsigned; notarization probably needs the engine's `.so`/`.dylib` files signed too (untested) | When a signing certificate is available |
| Desktop app is large (670 MB, 184 MB DMG), mostly Pixar USD, OpenCV and SciPy | `--without-usdz` saves ~250 MB; trimming further is future work |
| No local AI generation (Automatic mode chooses photogrammetry or cloud) | Needs a capable NVIDIA GPU to develop and test |
| Photogrammetry tested only on synthetic photos; dense (CUDA) path never run | Needs real captures and an NVIDIA machine |
| Baking transfers base colour only (no normal/roughness/emissive maps); no retopology; without Blender, textured models over budget are not reduced | Later |
| Provider keys stored encrypted on disk, not in the OS keychain | Later |
| Several backend processes sharing one PostgreSQL queue: designed for, not tested | When a multi-worker deployment needs it |
| Tauri window not automated (no WebDriver on macOS); native Save dialog not clicked through by hand | Linux/Windows CI with tauri-driver |
| Draco-compressed GLBs: untested end to end. trimesh (the backend's file check) has no glTF Draco support, so such provider output would likely be rejected | Later |
| Main JS bundle ≈580 KB (three.js already split out) | Not done |
| Vertex-level editing, texture settings, KTX2 textures | Later |
| Viewer e2e runs in local Chrome; WebGL in CI's headless Chromium is unverified | First CI run |
| Light theme | Not planned |
