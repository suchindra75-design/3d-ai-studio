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
| 7 | Inspect geometry | `workspace.spec.ts` (triangles, bounds, hierarchy) | `test_mesh_and_preprocess.py`, `test_processing.py` (quality report) |
| 7b | Edit the model | `e2e/editing.spec.ts` (delete, mirror, centre, texture replace, UV layout, cut; saved version checked and exported) | `packages/editor/src/structure.test.ts` (cut volumes, watertight caps with holes, inside-out models, mirror, pivot, textures), `test_export_colours.py` (mirrored parts export outward), `test_processing.py` (inside-out check) |
| 8 | Export | `workspace.spec.ts` (GLB, STL, and FBX when Blender is installed) | `test_enhance_export.py` (every format) |
| 9 | Re-open the export | `workspace.spec.ts` (downloaded GLB/STL bytes checked); `draco.spec.ts` (Draco GLB decoded in the viewer) | `test_enhance_export.py` (trimesh / Pixar USD), `test_blender.py` (FBX re-imported in Blender), `test_export_colours.py` (colours, Z-up printing, Draco), `test_local_ai.py` (every export of a real AI model) |
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
