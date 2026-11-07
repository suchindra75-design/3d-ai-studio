# Testing

Every automated test here runs against real code: the real FastAPI app and
database, real files re-opened by independent readers, real COLMAP and Blender
where they are installed, a real browser for end-to-end tests. The only
stand-ins are for **paid external services**:

- the **mock provider** (`backend/ai3d/providers/mock.py`) simulates a cloud
  AI provider and returns a placeholder sphere. It is registered only when
  `AI3D_ENVIRONMENT=test`, labels everything it makes as `mock`, and never
  appears in production;
- **fake Tripo/Meshy HTTP servers** (`backend/tests/fakes.py`) answer the real
  adapters with recorded response shapes. The adapters have **not** been run
  against the real services (that needs a paid key).

## Suites and how to run them

| Suite | Command | What it covers |
|---|---|---|
| Backend + engine | `uv run pytest` | API, database, storage, jobs, providers, processing, exports, COLMAP/Blender (when installed), error reporting |
| Backend on PostgreSQL | `AI3D_TEST_DATABASE_URL=postgresql+psycopg://… uv run pytest` | the same tests on PostgreSQL ([contributing guide](CONTRIBUTING_GUIDE.md)) |
| Backend on S3 storage | `AI3D_TEST_S3_ENDPOINT=… uv run pytest` (see below) | the same tests with every file in an S3-compatible bucket |
| Frontend unit | `pnpm test` | components, hooks, API client, viewer and editor logic (Vitest + Testing Library) |
| Web end-to-end | `pnpm test:e2e` | the Next.js app in Chrome, desktop and phone viewports (Playwright) |
| Desktop UI end-to-end | `pnpm test:e2e:desktop` | the desktop UI against a backend that requires the per-launch token |
| Job workflows end-to-end | `pnpm test:e2e:workflows` | cancel, retry and provider errors through the UI |
| Real photo capture (photogrammetry) | `AI3D_REAL_CAPTURE_DIR=… AI3D_COLMAP_PATH=… uv run pytest backend/tests/test_real_captures.py -s` | a real capture through upload, overlap check, reconstruction, ground removal and every export; verified with AliceVision's Buddha dataset (67 photos, CC BY 4.0, github.com/alicevision/dataset_buddha), which is not in the repository |
| Local AI (real TripoSR) | `AI3D_LOCAL_AI_DIR=… uv run pytest backend/tests/test_local_ai.py` and `AI3D_LOCAL_AI_DIR=… pnpm test:e2e e2e/local-ai.spec.ts` | a real phone photo → model, through the API and the browser (needs `ai3d-local-ai install`) |
| Desktop shell | `cd apps/desktop/src-tauri && cargo test` | Rust: token, ports, downloads allow-list, file names, the real engine's lifecycle |
| Engine bundle | `pnpm bundle:engine` | builds the release engine and smoke-tests it (starts it with a token, lists exports) |

Optional dependencies decide what runs: OpenMVS is used only when
`AI3D_OPENMVS_DIR` points at its programs; local AI tests need `AI3D_LOCAL_AI_DIR`
pointing at an installed engine (other tests ignore an installed engine unless
it is set), COLMAP tests need `AI3D_COLMAP_PATH`
(or COLMAP on `PATH`), Blender tests need Blender 4.2+, error-reporting tests
need the `errors` extra, S3 tests need the `s3` extra and an endpoint. Missing
ones are **skipped with the reason**, never reported as passed. Run
`uv sync --all-packages --all-extras` for everything.

### S3-compatible storage locally

Any S3-compatible server works. SeaweedFS (Apache-2.0) is what this project is
tested with (the MinIO server image is no longer published on Docker Hub):

```bash
cat > /tmp/s3.json <<'EOF'
{"identities": [{"name": "test", "credentials": [{"accessKey": "ai3d-test-key", "secretKey": "ai3d-test-secret-only-for-tests"}], "actions": ["Admin", "Read", "Write", "List", "Tagging"]}]}
EOF
docker run -d --name ai3d-test-s3 -p 127.0.0.1:8333:8333 \
  -v /tmp/s3.json:/etc/seaweedfs/s3.json:ro chrislusf/seaweedfs:latest \
  server -s3 -s3.config=/etc/seaweedfs/s3.json -dir=/data
AI3D_TEST_S3_ENDPOINT=http://127.0.0.1:8333 AI3D_TEST_S3_ACCESS_KEY_ID=ai3d-test-key \
  AI3D_TEST_S3_SECRET_ACCESS_KEY=ai3d-test-secret-only-for-tests uv run pytest
```

Each test uses its own prefix in the `ai3d-test` bucket.

## The workflows, and where each is tested

| # | Workflow | Browser (Playwright) | Backend / desktop |
|---|---|---|---|
| 1 | Create a project | `e2e/projects.spec.ts` · project lifecycle; `e2e/desktop-shell/token.spec.ts` | `test_projects.py` |
| 2 | Upload a photo | `projects.spec.ts` (valid, unsupported, duplicate); `token.spec.ts` (thumbnail through the token) | `test_images.py`, `test_image_analysis.py` |
| 3 | Start a generation | `e2e/generation.spec.ts` · explicit consent; waiting for permission; `e2e/local-ai.spec.ts` (local, no consent) | `test_generation.py` (consent, idempotency, routing), `test_local_ai.py` (local routing, offered fallback), `test_resources.py` (memory and disk checks), `job-list.test.tsx` (fallback buttons and consent) |
| 4 | Monitor progress | `generation.spec.ts` (live status); `workflows/jobs.spec.ts` (running → result) | `test_generation.py::test_event_stream` (Server-Sent Events), `job-list.test.tsx` |
| 5 | Receive the model | `generation.spec.ts` (model card, preview, GLB bytes); `local-ai.spec.ts` (**real AI model from a real photo**) | `test_generation.py`, `test_cloud_providers.py` (Tripo/Meshy fakes), `test_local_ai.py` (real TripoSR run) |
| 6 | Open the 3D viewer | `e2e/workspace.spec.ts` (real WebGL canvas); `token.spec.ts` | `packages/viewer` unit tests |
| 6b | View tools | `e2e/viewer.spec.ts` (display modes, lighting, section, measuring on the surface, PNG screenshot, side-by-side comparison; a 3.1-million-triangle model opened, timed and its JS heap bounded); `e2e/viewer-rendering.spec.ts` (on rendered pixels: lighting, flat shading without normals, Normals and UV views, section, measuring 0.97–1.00 across a 1.000 box, 2× screenshot size, linked cameras) | `packages/viewer/src/inspect.test.ts` (normals/UV views, flat shading without normals, bounds limit), `src-tauri/src/files.rs` (PNG-only saving), `test_mesh_and_preprocess.py` (row keys match NumPy) |
| 6i | Health inspection and repair | `e2e/inspection.spec.ts` (a box with a missing side: inspected, the hole shown in the same canvas without moving the camera, located, filled into a new watertight version after confirming, compared, and the original made current again) | `test_inspect.py` (clean, seams, holes, fins, degenerate and duplicate triangles, winding, inside-out, pieces, self-intersections against a check of every pair (large triangles among small ones too; an incomplete search is reported), capped locations, wall thickness on boxes, plates and a noisy box of known thickness, every thickness ray reaching the far wall on mixed sizes, printing checks with and without scale, overhangs, internal shells, UVs, invalid references in a file trimesh cannot open, Markdown), `test_repair.py` (each repair on a known case, UVs kept, pieces only when named, non-manifold limits; API: background inspection and downloads, failed inspection and memory refusal, repair as a new version and undo, a failed repair saves nothing, an interrupted one runs once), `packages/studio/src/workspace/inspection-panel.test.tsx` (grouping, methods, show and locate, confirmation before repairs, destructive removal), `test_memory_estimates.py` (the inspection estimate above every measured peak). Speed and memory at 1M–10M: BENCHMARKS.md#inspection-2026-11-08 |
| 6h | Smoothing, exports and hand-over at scale | `e2e/lod.spec.ts` (three preview-to-full hand-overs: same canvas, no context loss, no frame without the model, camera kept, same GPU geometry each time; then every level, picking, measuring, section) | `test_processing.py` (smoothing refused, not switched, when memory is short, both methods), `test_memory_estimates.py` (smoothing, GLB and FBX estimates above every measured run), `test_enhance_export.py` (an interrupted export is marked failed at start-up and its leftovers removed; two exports at once keep their own folders), `test_benchmark_scripts.py` (machine report; the noisy cube is closed, outward and of known size). Old/new equivalence of smoothing: measured, BENCHMARKS.md |
| 6g | Large models: memory, resilience, picking | `e2e/viewer-memory.spec.ts` (switching versions keeps GPU geometry/texture counts level; heap checked on a production build: `-c playwright.production.config.ts`) | `test_lod.py` (pieces without gaps, levels refused without free memory, a failed build changes nothing and retries, no level when nothing can be removed, the server answers within 0.5 s during a 1.3M-triangle build), `test_model_versions.py` (an interrupted upload saves nothing and leaves no files), `test_enhance_export.py` (an export that would not fit in memory is refused, nothing recorded), `test_memory_estimates.py` (the check, levels and export estimates stay above every measured peak in BENCHMARKS.md, soups included), `packages/viewer/src/pick.test.ts` (same nearest hit as three.js on 200 rays, indexed and not, front- and double-sided; index rebuilt after an edit; built ahead of time in idle periods, pausing about every 65,536 items) |
| 6f | Memory before the file checks | — | `test_mesh_and_preprocess.py` (triangles, vertices and textured vertices read from the GLB header match the loaded model; a model too large for free memory is refused with the numbers; chunked volume and degenerate counts match trimesh), `test_model_versions.py` (an upload refused for memory says so: 422 `not_enough_memory`, nothing saved) |
| 6e | Large-model benchmarks | `e2e/benchmarks/large-models.spec.ts`, `e2e/benchmarks/rendering.spec.ts` (opt-in: `AI3D_BENCHMARK_GLBS`; GPU timer queries, `AI3D_BENCHMARK_GPU`, `AI3D_BENCHMARK_DOWNLOAD_MBPS`) | `infrastructure/scripts/benchmark_geometry.py`; results in [BENCHMARKS.md](BENCHMARKS.md) |
| 6d | Levels of detail | `e2e/lod.spec.ts` (1.3-million-triangle model: levels built and loaded, Auto picks the coarsest far away and full detail close up, level 2 differs from full detail in under 3% of pixels, picking still selects the full model, the stored coarse level is shown while the full file loads) | `test_lod.py` (level counts; parts, placement, textures, UVs and colours kept; triangle soups simplified; a level that breaks the shape is dropped; API builds once and serves the files), `packages/viewer/src/lod.test.ts` (level choice with hysteresis, part matching, layers, following moved/hidden/deleted parts, edited parts drawn in full, repeatable dispose) |
| 6c | Problems on the model | `e2e/viewer.spec.ts` (a box with a hole shows the server's 4 open edges; timed on 3 million triangles) | `packages/viewer/src/problems.test.ts` (seams are not holes, a hole's rim, a three-way edge, world space) |
| 7 | Inspect geometry | `workspace.spec.ts` (triangles, bounds, hierarchy) | `test_mesh_and_preprocess.py`, `test_processing.py` (quality report) |
| 7b | Edit the model | `e2e/editing.spec.ts` (delete, mirror, centre, texture replace, UV layout, cut; saved version checked and exported) | `packages/editor/src/structure.test.ts` (cut volumes, watertight caps with holes, inside-out models, mirror, pivot, textures), `test_export_colours.py` (mirrored parts export outward), `test_processing.py` (inside-out check) |
| 7c | Enhance surface | `e2e/enhance-surface.spec.ts` (smoothing that keeps sharp edges on a noisy cube, opened with *Compare before/after*: mean distance to the true cube at the edges half of Taubin's, checked in the saved GLB; rounding (Taubin) halves the bumps on a rough sphere and keeps volume and colours; QuadriFlow rejected at 300 quads with the reason shown; with Blender, kept at 5,000 quads with normal and occlusion maps, colours carried over; checked in the saved GLB) | `test_blender.py` (QuadriFlow through the Enhance API: shape, colours, normal and occlusion maps; voxel remesh rebakes the texture), `test_processing.py` (smoothing keeps volume and seams; keeping sharp edges vs Taubin on a noisy cube at 3/10/30 passes: noise, edges, corners, volume; UVs unchanged; a drifting remesh is refused; only requested steps are reported as not applied), `test_blender.py` (a rejected retopology says why), `test_export_colours.py` (16-bit colours, colours beside textures survive re-saving), `panels.test.tsx` |
| 7d | Re-texture (cloud) | — (real services: `test_real_providers.py`, opt-in, see AI_PROVIDERS.md) | `test_cloud_providers.py` (Meshy sends the version's GLB as a data URI with the prompt; Tripo uses its own task id and refuses other models; nothing sent without permission; local-only projects must allow it), `panels.test.tsx` (upload must be confirmed, prompt required) |
| 7e | AI texture upscaling | `e2e/local-ai.spec.ts` (with `AI3D_LOCAL_AI_DIR`: ×2 from the Enhance tab on a real-photo texture, 256 → 512 px in the saved GLB, caveat shown) | `test_local_ai.py` (step enlarges within the size limit, unavailable until installed; with `AI3D_LOCAL_AI_DIR`: the real upscaler makes a detailed patch of a real photo 4× larger and sharper than Lanczos, and small tiles give the same image as one big tile); input limit, free-memory check and tile size from settings |
| 7f | Texture adjustments | `e2e/adjust.spec.ts` (saturation, tiling and vertex-colour brightness, checked in the saved GLB) | `packages/editor/src/adjust.test.ts` (colour maths, vertex colours in sRGB, tiling on copied geometry, undo) |
| 8 | Export | `workspace.spec.ts` (GLB, STL, and FBX when Blender is installed); `enhance-surface.spec.ts` (with Blender: an OBJ of a QuadriFlow result holds only quads, as many as the Quality tab shows) | `test_enhance_export.py` (every format), `test_quads.py` (quads found by position with shuffled faces and mixed diagonals, OBJ rewrite keeps winding, stale pairs dropped), `test_blender.py` (QuadriFlow quads kept in OBJ and FBX, counted after re-opening; GLB/STL and browser-edited versions are triangles) |
| 9 | Re-open the export | `workspace.spec.ts` (downloaded GLB/STL bytes checked); `draco.spec.ts` (Draco GLB decoded in the viewer) | `test_enhance_export.py` (trimesh / Pixar USD), `test_blender.py` (FBX re-imported in Blender), `test_export_colours.py` (colours, Z-up printing, Draco), `test_local_ai.py` (every export of a real AI model) |
| 9b | Import, back up, restore | `e2e/transfer.spec.ts` (STL import through the dialog, backup download, restore from the Projects page) | `test_transfer.py` (STL units and axes, `test_blender.py`: FBX round trip, OBJ+MTL+texture ZIP, PLY colours, unsafe/broken files refused, backup → restore keeps version history, damaged backups restore nothing), `src-tauri/src/files.rs` (backup path allowed, nothing else under projects) |
| 10b | Several workers, one queue | — | `test_shared_queue.py` (4 workers race over 40 jobs, each claimed once; 2 workers run 6 jobs to the end, one model each; SQLite here, PostgreSQL in CI) |
| 10 | Restart the application | — | `test_restart_recovery.py` (real server process killed with SIGKILL, restarted); `src-tauri/tests/sidecar.rs` (engine restarted after SIGKILL, clean stop) |
| 11 | Recover project and job history | `workflows/jobs.spec.ts` (history after reload) | `test_restart_recovery.py` (project, photo, finished model and job history after restart; interrupted job resumes the same provider task without sending photos again); `test_generation.py` recovery tests |
| 12 | Cancel and retry | `workflows/jobs.spec.ts` (cancel a running job, retry it) | `test_generation.py` (cancel while running, unsupported cancel, retry) |
| 13 | Provider errors | `workflows/jobs.spec.ts` (error shown, retry succeeds) | `test_generation.py` (failures, rate limits), `test_cloud_providers.py` (each provider's error mapping) |

Checked by hand (not automated): the release desktop app on macOS — start-up
with the bundled engine, token-authenticated requests, normal Quit stopping the
engine, relaunch. The Tauri window itself cannot be automated on macOS
(Tauri's WebDriver supports Linux and Windows only).

## Writing tests

- Prefer the real thing over a fake; fake only paid or remote services.
- A test that needs an optional tool skips with a reason that says how to
  enable it (`pytest.skip("set AI3D_COLMAP_PATH …")`).
- Process-level tests (`test_restart_recovery.py`, the Playwright suites)
  configure the mock provider through environment variables:
  `AI3D_MOCK_POLLS_UNTIL_DONE` (longer tasks) and `AI3D_MOCK_FAIL_FIRST_TASK=1`
  (the first task for each set of photos fails).
- Never report a test as passing unless it ran; CI summaries list skips.
