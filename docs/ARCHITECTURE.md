# AI 3D Studio — Architecture

This document records the system structure and the decisions behind it. When
a decision changes, update the relevant section and the "Decision log".

## 1. System overview

```
┌──────────────────────────┐      ┌──────────────────────────┐
│  apps/web  (Next.js)     │      │  apps/desktop (Tauri 2)  │
│  landing + studio routes │      │  Vite + React SPA        │
└────────────┬─────────────┘      └────────────┬─────────────┘
             │  both render the same screens from packages/studio
             │  through a small navigation adapter
             ▼                                 ▼
      @ai3d/api-client  ── typed REST + SSE ── (HTTP, /api/v1)
                                   │
┌──────────────────────────────────▼──────────────────────────────────┐
│ backend/ai3d  (FastAPI)                                             │
│  api/ ─► services/ ─► database/ (SQLAlchemy 2, Alembic)             │
│               │   └─► storage/ (local FS | S3-compatible)           │
│               ├─► processing/ (image analysis, mesh processing)     │
│               ├─► pipelines/  (single-image, multi-view, enhance)   │
│               │     └─► providers/ (Tripo, Meshy, Local, Mock)      │
│               └─► workers/   (persistent DB-backed job runner)      │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ Python import (same machine)
                     ┌──────────▼───────────┐
                     │ local-engine/        │  hardware detection,
                     │ ai3d_engine          │  COLMAP/Open3D/Blender
                     └──────────────────────┘  runners (no shell API)
```

## 2. Repository layout

```
.
├── apps/
│   ├── web/              Next.js App Router (landing page + studio routes)
│   └── desktop/          Tauri 2 shell: Vite + React entry, src-tauri/ (Rust)
├── packages/
│   ├── shared-types/     Types generated from the backend OpenAPI schema
│   ├── api-client/       Framework-agnostic typed client (fetch + XHR upload)
│   ├── ui/               Design tokens (CSS) + shadcn-style primitives
│   ├── utilities/        Formatting and client-side validation helpers
│   ├── studio/           Shared screens, TanStack Query hooks, Zustand stores
│   ├── viewer/           three.js / React Three Fiber viewport, GLB loading,
│   │                     scene facts (counts, hierarchy, exact bounds)
│   └── editor/           Undo/redo history, edit commands, GLB export, gizmo
├── backend/
│   ├── ai3d/             Python package (api, core, database, models,
│   │                     schemas, services, storage, processing, …)
│   └── tests/
├── local-engine/
│   └── ai3d_engine/      Python package used by backend workers: hardware
│                         detection, restricted tool runner, reconstruction/
│                         (COLMAP pipeline, model reader, upright alignment)
├── infrastructure/
│   ├── docker/           Dockerfiles
│   └── scripts/          Dev and maintenance scripts
├── e2e/                  Playwright end-to-end tests
├── docs/
├── pyproject.toml        uv workspace root (backend + local-engine)
├── pnpm-workspace.yaml   pnpm workspace (apps/*, packages/*)
└── docker-compose.yml
```

### Deviations from the initial suggested structure (and why)

| Change | Reason |
|---|---|
| Added `packages/studio` | Web (Next.js) and desktop (Vite) must render identical screens. Screens are routing-agnostic React components; each app injects a navigation adapter (`next/link` vs `react-router`). Without this package every screen would be written twice. |
| Backend code lives in the `backend/ai3d` package, not directly in `backend/` | `api`, `core`, `models` as top-level Python modules would collide with third-party packages and cannot be installed as a wheel (needed for the desktop sidecar in Phase 6). |
| `local-engine/gpu-detection` → `ai3d_engine/hardware` | Hyphenated directories cannot be Python modules; the module also detects CPU/RAM/disk/dependencies, not only GPUs. |
| `packages/viewer`, `packages/editor` created in Phase 3 | Empty packages would be placeholders; they were created with their real code. The studio loads them (and three.js) lazily, only when a model is opened. |
| Top-level `e2e/` | Playwright tests exercise the backend and web app together, so they belong to neither. |

## 3. Technology choices

| Area | Choice | Notes |
|---|---|---|
| JS package manager | pnpm workspaces (via Corepack) | Strict dependency isolation; `packageManager` field pins the version. |
| Web | Next.js (App Router), React 19, TypeScript, Tailwind CSS v4 | Next.js only renders the web app. API calls go through a Next.js rewrite to the backend so the browser stays same-origin. |
| Desktop | Tauri 2, Rust, Vite, React Router (hash routing) | Separate Vite entry — no Next.js server inside Tauri. |
| UI primitives | shadcn/ui pattern (Radix + CVA + tailwind-merge), lucide icons | Components are owned source in `packages/ui`, not a runtime dependency. |
| State | TanStack Query (server state), Zustand (UI state, upload queue) | |
| 3D (Phase 3) | three, @react-three/fiber, @react-three/drei | |
| Python | 3.11–3.12, managed with **uv** (workspace + lockfile) | Upper bound <3.13 because Open3D/PyTorch wheels lag new Python releases. |
| API | FastAPI, Pydantic v2, pydantic-settings | |
| DB | SQLAlchemy 2 (sync sessions) + Alembic | Sync sessions run in FastAPI's threadpool; simpler and robust with SQLite; the job runner uses the same models. |
| Images | Pillow (decode/validate/EXIF/thumbnail), OpenCV headless (analysis) | |
| Mesh / reconstruction (Phase 4–5) | trimesh, Open3D, pycolmap/COLMAP, Blender (`bpy` or CLI) | Optional extras; hardware detection reports which are installed. |
| Lint/format | ruff + mypy (Python); ESLint 9 flat config + Prettier (TS); clippy/rustfmt (Rust) | |
| Tests | pytest + httpx; Vitest + React Testing Library; Playwright | See [TESTING.md](TESTING.md). |
| Storage | Data folder, or S3-compatible (boto3, optional) | |
| Error reporting | sentry-sdk (optional, off by default) | Errors only, scrubbed; [ERROR_REPORTING.md](ERROR_REPORTING.md). |
| Release | uv's python-build-standalone + `tauri build`; GitHub Actions | [RELEASING.md](RELEASING.md). |

## 4. Backend structure

```
ai3d/
├── main.py            create_app(): middleware, routers, error handlers, lifespan
├── core/              config (Settings), logging (JSON), errors (AppError), ids/time
├── database/          engine/session factory, SQLite pragmas, Base, migrations/
├── models/            SQLAlchemy ORM models (one module per aggregate)
├── schemas/           Pydantic request/response models (the API contract)
├── api/v1/            Routers: health, system, projects, images, devices, …
├── services/          Use-case logic; routers stay thin; no FastAPI imports
├── storage/           StorageBackend protocol, LocalFileStorage, S3Storage, key layout
├── processing/        Image validation/analysis, provider photo copies, basic mesh checks
├── providers/         Provider protocol + Tripo, Meshy and test-only mock adapters
└── workers/           DB-backed job runner and the generation pipeline
```

`pipelines/` (multi-view reconstruction) arrives in Phase 4.

**Layering rule:** `api → services → (database, storage, processing,
pipelines)`. Services never import FastAPI; providers never touch the database
or HTTP request objects; pipelines orchestrate providers + processing and
report progress through a callback interface the worker persists as job events.

### 4.1 Configuration
`ai3d.core.config.Settings` (pydantic-settings, prefix `AI3D_`), read from
environment and `.env`. Secrets (provider API keys) are `SecretStr` and are
never serialised in responses or logs.

### 4.2 Database
- SQLite by default at `<data_dir>/ai3d.sqlite3` with `journal_mode=WAL`,
  `foreign_keys=ON`, `busy_timeout`. PostgreSQL via `AI3D_DATABASE_URL`.
- Portable types only: `sqlalchemy.Uuid` (UUIDv4 primary keys), `JSON` (JSONB
  variant on PostgreSQL), non-native enums stored as strings, timezone-aware
  UTC timestamps via a `UTCDateTime` type decorator.
- Naming convention for constraints so Alembic batch migrations work on SQLite.
- Migrations live inside the package (`ai3d/database/migrations`) so an
  installed backend (desktop sidecar, Docker) can migrate itself on start-up
  (`AI3D_AUTO_MIGRATE=true`).
- Binary data never goes in the database — only storage keys.

See `DATABASE_SCHEMA.md`.

### 4.3 Storage
`StorageBackend` protocol: `save(key, stream) → StoredObject`, `open`,
`exists`, `delete`, `delete_prefix`, `local_path` (optional). `LocalFileStorage`
writes atomically (temp file + rename) under `<data_dir>/storage` and rejects
keys that escape the root. Key layout:

```
projects/{project_id}/images/{image_id}/original.{ext}
projects/{project_id}/images/{image_id}/thumbnail.webp
projects/{project_id}/models/{model_id}/versions/{version_id}/...
projects/{project_id}/exports/{export_id}/...
```

`S3Storage` (optional `s3` extra, `AI3D_STORAGE_BACKEND=s3`) keeps the same
keys under `AI3D_S3_PREFIX` in one bucket of any S3-compatible service; each
upload is one PUT (atomic in S3). It returns no `local_path`, so downloads are
streamed through the backend (the bucket stays private) and processing copies
objects to a temporary folder first. Both backends share the key-safety rules
(`storage.keys.validate_key`) and pass the same contract tests; the whole
backend suite also runs with S3 storage (`AI3D_TEST_S3_ENDPOINT`, see
[TESTING.md](TESTING.md)).

### 4.4 Job runner (implemented in Phase 2)
A persistent, DB-backed queue — no Redis required for local installs:

1. `generation_jobs` rows are created `queued` (with an optional unique
   `idempotency_key`).
2. Worker threads claim a job with an atomic `UPDATE … WHERE status='queued'`
   and set `lease_owner` / `lease_expires_at`; they heartbeat while running.
3. Every stage transition, progress update, warning and error is appended to
   `job_events` (monotonic `seq` per job) — the SSE endpoint streams these and
   supports `Last-Event-ID` resume; polling reads the same table.
4. On start-up, jobs whose lease expired are re-queued if their current stage
   is resumable (provider task id already stored → resume polling instead of
   paying for a new generation) or failed with `interrupted` otherwise.
5. Cancellation sets `cancel_requested`; the worker checks it between stages
   and calls the provider's cancel API when supported.

Celery + Redis are **not** used (D27): nothing needs them yet. Claims are
atomic `UPDATE`s with leases, so several backend processes sharing one
PostgreSQL database are designed to share the queue safely — but that set-up
has not been tested. The pipeline and provider code does not depend on the
runner, so a distributed runner can replace it when a deployment needs one.

### 4.5 API conventions
- Base path `/api/v1`; OpenAPI at `/api/v1/openapi.json`, docs at `/api/docs`.
- Errors always have the shape
  `{"error": {"code", "message", "details", "request_id"}}`.
- Every response carries `X-Request-ID`.
- List endpoints return `{"items": [...], "total", "limit", "offset"}`.
- Idempotency: job creation accepts an `Idempotency-Key` header; uploads are
  de-duplicated by content hash per project.
- Progress: `GET /jobs/{id}/events` (SSE) with `GET /jobs/{id}` polling
  fallback.

## 5. Frontend structure

- `@ai3d/studio` exports screens (`ProjectsScreen`, `ProjectWorkspaceScreen`,
  `NewProjectScreen`, `SettingsScreen`, `AboutScreen`), the `AppShell`, query
  hooks and stores. It depends on a `StudioProvider` that supplies:
  - an `ApiClient` instance (base URL differs per app),
  - a navigation adapter `{ Link, push, replace }`,
  - platform info (`web` | `desktop`).
- `apps/web` maps Next.js routes to these screens and owns the landing page.
- `apps/desktop` maps hash routes (`react-router`) to the same screens.
- Server types come from `@ai3d/shared-types`, generated from the backend's
  OpenAPI schema (`pnpm gen:api`); CI fails if the generated file is stale.

### Design language
Dark-first, neutral greys, one restrained accent, system/Inter typography,
1px hairline borders, minimal shadows, no gradients or glass effects.
Tokens are CSS variables in `packages/ui/src/styles/theme.css` consumed by
Tailwind v4 `@theme`, so web and desktop share them.

## 6. Desktop and local engine

- **Sidecar** (`apps/desktop/src-tauri/src/sidecar.rs`): the app starts
  `ai3d-server` with `--host 127.0.0.1 --port <random free port>` and the
  environment `AI3D_API_TOKEN` (64 random hex characters, new each launch),
  `AI3D_DATA_DIR` (the app's data folder), `AI3D_LOG_FORMAT=json` and
  `AI3D_PARENT_PID`. A supervisor thread waits for `/api/v1/health`, marks the
  engine *running*, and restarts it with back-off if it exits (at most 3
  times in a row, then *failed* with the log tail). Quitting sends SIGTERM and
  kills after 10 s.
- **Which engine**: `AI3D_DESKTOP_BACKEND`, else `resources/backend/ai3d-server`
  in the app bundle, else (debug builds only) the repository's
  `.venv/bin/ai3d-server`, run from the repository so its `.env` applies.
- **Window ↔ engine**: the window asks Rust for `backend_status` and talks to
  the engine over HTTP with `Authorization: Bearer <token>`; URLs that the
  browser loads itself (`<img>`, `<a download>`, `EventSource`) carry
  `?access_token=`. The CSP allows `http://127.0.0.1:*` for this.
- **Rust commands** (`lib.rs`): `app_info`, `backend_status`,
  `restart_backend`, `save_backend_file`, `open_data_folder`. There is **no**
  generic shell, process, file-system or HTTP command. `save_backend_file`
  only downloads paths under `/api/v1/model-versions/`, `/api/v1/exports/` or
  `/api/v1/images/` (no `..`, no query) to the place the user picks in the
  native Save dialog, writing through a `.part` file. The dialog and opener
  plugins are called from Rust; the window has no permissions for them.
- `ai3d_engine` runs external tools (COLMAP, Blender) only through fixed
  argument builders with validated paths — never with user-provided strings
  passed to a shell.

## 7. Security model

| Concern | Control |
|---|---|
| API keys | Server-side only (env or encrypted settings). Never sent to the browser, never logged, excluded from exports. |
| Local-only images | `processing_mode=local` jobs cannot be routed to a cloud provider; cloud fallback requires `cloud_upload_consented=true` on the job. |
| Uploads | Content-sniffed type allow-list, size limit, Pillow decompression-bomb limit, storage keys generated server-side (user filenames are metadata only). |
| Path traversal | Storage resolves and checks every key stays under its root. |
| Network exposure | Backend binds `127.0.0.1` by default; CORS restricted to known app origins. |
| Local engine port | Per-launch random token on every `/api/*` route except health (constant-time compare; never logged; query strings are not logged). Random port, `127.0.0.1` only. |
| Orphaned engine | `AI3D_PARENT_PID` watchdog: the engine stops when the app is gone. |
| Desktop | Five fixed Tauri commands; downloads restricted to an allow-list and the native Save dialog; strict CSP; no shell, fs or http plugin permissions. |

## 8. Decision log

| # | Decision | Status |
|---|---|---|
| D1 | Monorepo with pnpm (JS) + uv (Python) workspaces | Accepted (Phase 0) |
| D2 | Shared `@ai3d/studio` screen package with navigation adapters | Accepted (Phase 0) |
| D3 | Sync SQLAlchemy sessions; SQLite WAL default | Accepted (Phase 0) |
| D4 | DB-backed job runner before Celery/Redis | Accepted (Phase 0), implemented Phase 2 |
| D5 | Generated TS types from OpenAPI | Accepted (Phase 0) |
| D6 | Next.js rewrite proxy for web → backend (same-origin) | Accepted (Phase 0) |
| D7 | Python 3.11–3.12 only | Accepted (Phase 0); revisit when Open3D/PyTorch support 3.13+ |
| D8 | Apache-2.0 for project code; third-party models/weights never bundled | Accepted (Phase 0) |
| D9 | Workspace hierarchy and counts read from the GLB in the browser, not from `model_parts` | Accepted (Phase 3): the file is the source of truth and every version has one; `model_parts` stays for server-side processing (Phase 5) |
| D10 | Edits are exported to GLB in the browser (three.js `GLTFExporter`) and saved as a new `edited` version; the backend re-opens and checks the file like provider output | Accepted (Phase 3): no geometry code on two sides; originals are never modified |
| D11 | Lighting from three's built-in `RoomEnvironment`; Draco decoder from three's bundled assets | Accepted (Phase 3): the viewer downloads nothing from CDNs (local-first, works offline, fits the desktop CSP). Decoding a Draco file is not yet tested |
| D12 | Local photogrammetry registered as a provider (`local-photogrammetry`) but run in-process by the job worker, not through submit/poll | Accepted (Phase 4): planning, settings and job records stay uniform; a local run has no remote task to resume, so an interrupted run restarts from the photos |
| D13 | Upright orientation from camera x-axes (gravity ⟂ every camera's horizontal axis) instead of COLMAP's image-orientation aligner | Accepted (Phase 4): the aligner left an 18° tilt on downward-looking captures; the x-axis rule was exact on the test scene |
| D14 | Topology checks (pieces, watertight) on the vertex-merged shape | Accepted (Phase 4): texture seams split vertices and made every UV island look like a separate open piece |
| D15 | Exports are validated by re-opening with an independent reader (trimesh; Pixar USD for USDZ) and comparing triangles, size and texture; failures are never stored | Accepted (Phase 5) |
| D16 | STL and 3MF are written in millimetres (slicer convention); glTF/USDZ stay in metres | Accepted (Phase 5): writing glTF metres into a millimetre format made models 1000× too small |
| D17 | Steps that would silently discard textures (hole filling, decimation on textured meshes) or need missing tools (Blender) are reported as skipped/unavailable, never approximated | Accepted (Phase 5); with Blender, textured models over budget are now baked instead (D23) |
| D18 | Decimation via fast-simplification instead of Open3D | Accepted (Phase 5): same algorithm family; Open3D's import alone took 23 s here |
| D19 | The desktop engine is the normal backend started by Rust (`std::process`), not Tauri's shell plugin | Accepted (Phase 6): no shell permissions in the app at all; Rust owns the lifecycle and the token never passes through a command line |
| D20 | Token in `?access_token=` for URLs the browser loads itself | Accepted (Phase 6): `<img>`, downloads and `EventSource` cannot send headers; the engine never logs query strings |
| D21 | Desktop saves go through a Rust command with a path allow-list and the native Save dialog | Accepted (Phase 6): WebKit ignores `<a download>` for cross-origin URLs; the window gets no file-system access |
| D22 | Parent-PID watchdog in the engine | Accepted (Phase 6): found an orphaned engine after `tauri dev` restarted the app; the app cannot clean up when it is killed |
| D23 | Blender runs as a headless subprocess with a repository-owned script and three fixed commands (not `bpy` in the backend process) | Accepted (Phase 6): `bpy` wheels are tied to one Python version and are huge; a subprocess can be cancelled and timed out, and FBX is re-opened by a separate run |
| D24 | Release desktop builds bundle a relocatable CPython (python-build-standalone) with the backend installed from `uv.lock`, run as `python -I -m ai3d.cli`; not PyInstaller | Accepted (Phase 7): package data (Alembic migrations, trimesh resources, USD plugins, the Blender script) stays as real files, no hidden-import lists to maintain, and every dependency is installed with hash checking. Costs ~670 MB unpacked (184 MB DMG) |
| D25 | S3 storage streams downloads through the backend instead of presigned URLs | Accepted (Phase 7): the bucket stays private, the per-launch token and consent rules keep working, and the browser never talks to the storage service |
| D26 | Error reporting through sentry-sdk's logging integration only, with all web-framework integrations off and a scrubber on every string | Accepted (Phase 7): request data cannot leak because it is never collected |
| D27 | No Celery/Redis | Accepted (Phase 7): the master plan adds them "when distributed processing requires them"; nothing does yet, and the DB runner already survives restarts |
| D28 | The local AI engine runs in its **own** Python environment, started like COLMAP/Blender through a fixed runner (`ai3d_engine.local_ai`): fixed interpreter, repository-owned script, JSON request, timeout, cancel, minimal environment | Accepted (Phase 8): PyTorch 2.2.2 (the last Intel-Mac build) needs NumPy 1.x while the backend uses NumPy 2; isolation also keeps a 1 GB ML stack and third-party model code out of the backend process |
| D29 | TripoSR's compiled/optional imports are replaced by small stand-ins (PyMCubes for `torchmcubes`, returning its reversed axis order (fixed in Phase 11: models had been mirrored and inside out), our own U²-Net background removal instead of `rembg`); its Hugging Face lookups are redirected to verified local files; the checkpoint is loaded tensors-only | Accepted (Phase 8): no compiler needed, `rembg`'s numba dependency has no Intel-Mac build, and nothing is downloaded or executed at run time beyond the pinned, hash-checked install |
| D30 | One heavy local job at a time (COLMAP/OpenMVS, local AI, or Blender baking), shared lock in `workers/heavy.py` | Accepted (Phase 8; Blender baking added in Phase 10): each uses every CPU core; two at once only slows both and risks running out of memory. Enhance waits only around the Blender run |
| D31 | STL and 3MF are written Z-up (rotated +90° about X from glTF's Y-up); validation compares the swapped axes | Accepted (Phase 9): slicers and CAD tools treat Z as up; Y-up files opened lying on their back. Found on a real AI model |
| D32 | Per-vertex colours are a first-class property: tracked in export validation (`keeps_colour` per format), written as sRGB where the format expects it (OBJ, 3MF colour group, FBX) and linear where it does not (glTF, USDZ displayColor) | Accepted (Phase 9): local AI models are coloured per vertex; 3MF, USDZ and FBX dropped the colours and OBJ wrote them too dark, all silently |
| D33 | Every model file is loaded through `processing.mesh.load_model_scene`, which normalises glTF COLOR_0 next to a material | Accepted (Phase 9): trimesh keeps such colours as a float RGB side attribute that its own writer then fails on (seen with a Blender-made, Draco-compressed file) |
| D34 | DracoPy is a backend dependency (Apache-2.0) | Accepted (Phase 9): without it Draco-compressed GLBs, which providers may return, could not be validated at all; the viewer already decodes Draco with three.js' bundled decoder (now tested in e2e) |
| D35 | Dense photogrammetry without a GPU through OpenMVS, started as separate allow-listed programs (`reconstruction/openmvs.py`); preferred over COLMAP's CUDA dense step when installed | Accepted (Phase 9b): the sparse surface was unusable on a real capture; OpenMVS on the CPU gave a detailed surface in ~4.5 min. It is AGPL-3.0, so it is never linked or bundled; COLMAP's CUDA path has never been tested, OpenMVS has |
| D36 | "Up" for photogrammetry: the x-axis rule (D13) only when the cameras' x axes are clearly coplanar (smallest/middle singular value < 0.35); otherwise the mean direction from the object to the cameras | Accepted (Phase 9b): a real capture with portrait and top-down shots had x axes spread in 3D; the x-axis rule was 92° off, the camera-direction rule 5.6° from the table |
| D37 | Ground removal searches the lowest 20% of the model, and a plane only counts as ground when it is wide (≥ half the model's width) and almost nothing (≤ 3%) lies under it | Accepted (Phase 9b): on a dense real scan the board was only a few % of all points; the extra conditions keep a sphere's bottom from being cut off |
| D38 | Resource checks in `services/resources.py` from measured needs: too little disk blocks; under 8 GB installed memory makes local AI unavailable (Local mode blocks, Automatic uses the cloud with consent); low free memory only warns. The disk is checked again when a waiting job gets its turn | Accepted (Phase 10): a job that would fail half-way should not start, but a temporary memory shortage is the person's to fix by closing programs |
| D39 | Fallback between local and cloud is *offered*, never automatic: a failed job lists alternatives (`JobRead.alternatives`), and retrying with one starts a new job that goes through the normal consent rules; Local mode never offers the cloud | Accepted (Phase 10): switching to the cloud on its own would upload photos the person meant to keep local |
| D40 | Cutting with a plane runs in the browser editor (`packages/editor/src/cut.ts`) as an undoable command; caps are triangulated with three.js' Earcut; the server validates the saved version like any edit | Accepted (Phase 11): one geometry path for all workspace edits, cuts combine with other edits and undo; trimesh's slicer drops vertex colours and needs extra packages to cap |
| D41 | Mirroring is stored as a negative scale on the node, not baked into the vertices | Accepted (Phase 11): it stays an ordinary, reversible transform; glTF readers flip the faces back, and the STL/3MF exports were tested to bake it with outward faces |
| D42 | A cut's cap is wound against the outline's edges (not by the plane's normal), and its vertex colours/UVs come from the cut edge | Accepted (Phase 11): a cap that follows its neighbours keeps a mesh consistent whichever way its faces point; the inside of a model is unknown, so the edge colours are the honest guess |
| D43 | Section view, measuring and the cut preview use renderer clipping and overlays only; they never change the scene | Accepted (Phase 12): viewing tools must not become edits by accident; saving stays disabled while only viewing tools are used |
| D44 | Desktop screenshots go through one more fixed Rust command, `save_image`: raw PNG bytes (signature checked, at most 64 MB) written only to the path the user picks in the native Save dialog | Accepted (Phase 12): the window still has no file-system permissions; WebKit does not download blob URLs in the desktop web view |
| D45 | Exact (per-vertex) bounds only up to 500,000 vertices; above that, each part's own bounding box | Accepted (Phase 12): the exact pass made selecting a part on a 3-million-triangle model take ~2 s; part boxes are exact for unrotated parts and slightly larger for rotated ones |
| D46 | The web app's `/api/v1` proxy accepts request bodies up to 520 MB (`proxyClientMaxBodySize`) | Accepted (Phase 12): Next.js' default of 10 MB silently truncated uploads of large photos and models; the backend enforces the real limits |
| D47 | Edge and triangle uniqueness is computed on one int64 key per row (`processing.mesh.row_keys`), not `np.unique(axis=0)` | Accepted (Phase 12): 57 s → 21 s to validate a 6-million-triangle model; results are identical (tested against NumPy) |
| D48 | Retopology/remeshing runs as a fourth fixed Blender command (`remesh`) and its result is kept only when the two surfaces agree: 95th-percentile distance ≤ 1% and mean ≤ 0.3% of the bounding-box diagonal, measured both ways on 20,000 samples with Blender's BVH | Accepted (Phase 13): QuadriFlow can move open or damaged surfaces; a measured check is the honest way to offer it. No new Python dependency (trimesh's proximity queries need `rtree`) |
| D49 | GLB files are read with 16-bit COLOR_0 narrowed to 8-bit first (`processing.mesh.narrow_glb_colours`), and colours beside textures are kept as bytes | Accepted (Phase 13): trimesh wrapped 16-bit colours (Blender's output) to noise and wrote float colours as black; both were found by the colour check of a remeshed model |
| D50 | Imports go through `processing/importing.py`: ZIPs are unpacked with limits (500 entries, 1 GB, no absolute/`..`/symlink entries, one model), every format becomes a GLB that is re-opened and checked, and the version's provenance is `unknown` | Accepted (Phase 15): an uploaded archive is untrusted input; an imported model's accuracy is not something the app can vouch for |
| D51 | Backups are a ZIP with a versioned `manifest.json`; restore creates a new project with new IDs and re-runs every check (photos through the upload path, models through validation), rolling back completely on failure | Accepted (Phase 15): a backup may come from elsewhere or be damaged; nothing in it is trusted, and no existing project is ever overwritten |
| D52 | Re-texturing is a provider job kind (`JobKind.RETEXTURE`) on the generation pipeline: the input is the version's GLB (or, for Tripo, its own task id); consent works as for photos; the result is a new `enhanced` version | Accepted (Phase 14): reuses submit/poll/cancel/download, restart recovery and the consent rules instead of a second cloud path; Tripo is limited to its own models because its documented upload API takes images only |
| D53 | Texture upscaling uses Real-ESRGAN's compact model in the local-AI environment: our own copy of the network, weights loaded tensors-only, run through the engine's fixed-script runner; tiles are resized to the output size as they are made | Accepted (Phase 14): permissive licence (BSD-3-Clause), 4.9 MB, fast enough on a CPU (a 2048 px texture in about a minute); no `basicsr`/`realesrgan` packages and no code from the weights file |
| D54 | Texture adjustments are baked into data: colour into pixels (or vertex colours, adjusted in sRGB), tiling into the UVs of the parts using the material (copied geometry, textures set to repeat) | Accepted (2026-11-05): `KHR_texture_transform` and material tints would be lost by trimesh (Enhance, validation) and by the OBJ/STL/USDZ/FBX exports; baked data survives every path |
| D55 | High-resolution screenshots re-render the view once at a higher pixel ratio (1×/2×/4×), capped at 8192 px and the GPU's renderbuffer and viewport limits, then restore it | Accepted (2026-11-05): no second renderer or scene copy; the cap keeps a 4× capture on a large screen from failing or exhausting GPU memory |
| D56 | AI texture upscaling limits are settings (`upscale_max_input_px`, `upscale_tile_px`), and each texture is checked against an estimate of the memory it needs (about twice the measured peak) before it runs; a texture that does not fit is kept, with the numbers | Accepted (2026-11-05): the right limits depend on the machine; a failed or swapping job is worse than a clear "not enough free memory" |
| D57 | Requested Enhance steps that do not apply are stored as the job's `warnings` ("Title: reason") and shown with the result; routine checks that find nothing to do are not | Accepted (2026-11-05): a fidelity rejection or a missing tool is an answer the user needs; "nothing needed changing" was wrong when something was asked for and refused |
| D58 | Levels of detail: the backend builds simplified copies (each ¼ of the previous, down to 30,000 triangles) of versions over 500,000 triangles on first request and stores them as `lod_mesh` assets; the viewer draws a copy instead of the full parts when the model would get more than ~2 front-facing triangles per pixel, moving full parts to a hidden render layer. Picking, measuring, editing and saving always use the full model; screenshots render full detail | Accepted (2026-11-06): the original stays the only source of truth; copies follow each part's transform, material and visibility, and an edited part is drawn in full. Quadric collapse (`fast-simplification`, already a dependency) with borders kept preserves texture seams; a level that moves the bounds by >1% or vertices by >3% on average is dropped |
| D59 | Smoothing defaults to bilateral normal filtering (Zheng et al. 2011: face normals averaged over faces sharing a vertex, weighted by distance and by normal difference with σ = 0.35, then vertices fitted to the normals), with Taubin kept as "Round them off"; both run on the shape with seams merged, so UVs and colours are untouched | Accepted (2026-11-06): on noisy test shapes with known true surfaces it removes noise better and keeps edges and corners (cube at 10 passes: half of Taubin's error at the edges); simple sharp-edge locking was tried first and failed, because noise itself makes edges over 40°. Bumps as large as the triangles are taken for features, so very rough organic surfaces still need Taubin |
| D60 | Quads from retopology are kept beside the triangle mesh: Blender writes each quad's corners, the backend matches them to the two GLB triangles that cover it (by position) and stores the pairs with the version (`quad_topology` asset, `stats.quads`); OBJ and FBX exports join the pairs back into quads, and the re-opened file's quad count is reported. The GLB itself stays triangles | Accepted (2026-11-06): glTF has no quads, and the viewer, editor and every other path work on triangles; storing the pairing keeps one source of geometry and only marks which triangles belong together. Nothing is inferred from triangle shapes; a version edited in the browser has no pairs, so its exports are honestly triangles |
| D61 | The upload check runs in stages (scene facts, then positions, then topology on the merged shape), releasing each stage's data; edge use, watertightness and winding come from one sort of integer edge keys; volume, degenerate triangles and centres are computed in chunks. Before it, and before building levels or exporting, the memory needed is estimated from the GLB header (triangles, vertices, vertices with texture coordinates, decoded texture sizes) and the work is refused with the numbers if it would not fit in free memory; each estimate is checked against every measured run in `tests/test_memory_estimates.py` | Accepted (2026-11-07): same metrics as before on every case compared (inside-out, open, soup, non-manifold, duplicates, textures, parts, the 10M scan), 8.4 → 3.7 GB on 10 million triangles; refusing with a clear message is better than swapping or a crash, and nothing is recorded, so it can be retried. Estimates from triangles alone were below the measured peak for triangle soups (unshared vertices) and for textured OBJ exports, so vertices count too |
| D62 | Levels of detail simplify meshes over 2.5 million triangles in pieces (slabs along the longest side), each keeping its cut edges; attributes are averaged per piece | Accepted (2026-11-07): the simplifier's own memory grows with the mesh it gets; pieces halved the peak (6.6 → 3.6 GB on 10 million triangles) with the same accuracy; the cuts keep full density, which costs a few hundred triangles |
| D63 | Rendering is measured with WebGL2 timer queries around `renderer.render()` through an opt-in hook (`window.__ai3dViewerProbe`, inert unless a test creates it), reading the GPU from the app's own context | Accepted (2026-11-07): frame timing through animation frames measured the display loop, not the GPU (the first attempt was invalid); a new canvas can be given another GPU than the app's (Chrome picked the Intel GPU for a test canvas while the app, which asks for high performance, drew on the Radeon) |
| D64 | Picking on meshes over 200,000 triangles goes through a per-geometry index: triangles sorted along a Morton curve into chunks of 2,048 with bounding boxes, kept beside the geometry (its triangle order is never changed); the nearest hit only is reported. Built after loading in idle time, a few milliseconds at a time (or at once if a click comes first), rebuilt when the geometry or its positions change | Accepted (2026-11-07): three.js tested every triangle (about 1.15 s per click on 10 million); same nearest hit as three.js on every compared ray; building at the first click made that click take 2.2 s on 10 million triangles; no new dependency (three-mesh-bvh would also do this) |
| D65 | Levels of detail are built in the background, one at a time, each in a separate process; the POST returns `building` at once and the viewer polls | Accepted (2026-11-07): the simplifier holds Python's interpreter lock for seconds at a time, which froze the server (connections reset during large builds; 2.5 s request stalls even at 1.3 million triangles); a build taking minutes also outlasted the web proxy's 10-minute limit; the process's memory is returned when it ends |
| D66 | Feature-preserving smoothing keeps the same algorithm (D59) but stores each face's neighbours once, as compressed rows (4-byte indices) with a single-precision fixed spatial weight, and runs every step a block of faces at a time; watertightness and volume come from the low-memory checks (D61) instead of trimesh's caches. Splitting the model into spatial regions or components was not needed and was not done | Accepted (2026-11-07): the same result as the old code on the same models (largest difference 2.6 × 10⁻⁹ of the model's size, Statuette), with 2.5–2.7× less peak memory (10M cube 8.6 → 3.4 GiB) and 1.3–1.8× less time after profiling (a face's own values repeated sequentially, corner offsets as n·c̄ − n·cₖ). Regions would change the neighbourhoods at their borders, which is where seams would appear. Both methods now refuse, with the numbers, when free memory is short, instead of switching method |
| D67 | The simplified preview and the full model share one canvas: the preview is the same `Viewport` element, given the full model when it arrives, with a `frameKey` (the version) so the camera is framed once per version, not once per model object | Accepted (2026-11-07): two canvases meant two WebGL contexts in turn (releasing the first took 0.15–0.6 s); measured on the Statuette, the hand-over's longest task went from about 0.99 s to 0.36–0.5 s and all its long tasks from about 2.0 s to 0.9–1.5 s; what remains is parsing the full file and compiling its shaders, there in both. The coarsest level stays on the GPU for the levels of detail, and a camera turned during the preview stays. React Three Fiber never disposes `<primitive>` objects, so the preview's geometry stays with the levels that own it |
| D68 | Exports free the model before re-opening the written file (the check compares against facts taken beforehand), write OBJ text into the zip in pieces, and count OBJ quads line by line; for FBX the model is written to a GLB for Blender and freed before Blender starts. GLB and FBX have their own memory estimates (FBX includes Blender). Each export records the process writing it; at start-up, exports left "running" by a process that is gone are marked failed ("interrupted") and their temporary folder and any unrecorded stored file are removed | Accepted (2026-11-07): the same checks (triangles, size, texture, colours, quads) on the re-opened file; nothing weaker; 6–22% less memory for GLB and OBJ. Blender's own memory dominates FBX (up to 8.6 GB at 10M) and is outside the app's control; giving Blender the stored GLB instead made it use more and was dropped. An export interrupted by a crash otherwise stayed "running" for ever. Other computers sharing the database: after 6 hours |
| D69 | A health inspection (`processing/inspect.py`) reports findings by category (geometry integrity, topology, surface, UV and texture, optional fabrication), each with severity, an exact count, how it was measured (exact, approximate, sampled), capped world-space locations and the repair that addresses it. It runs on request in a separate process and is kept as a report file with the version (JSON and Markdown downloads). No overall score | Accepted (2026-11-08): numpy and scipy only (Open3D is an optional extra, absent from CI and the default image); one uniform grid serves the self-intersection pairs and the thickness rays; self-intersections checked against a test of every pair. A score would mix unlike problems; each finding states its limits instead (docs/MESH_INSPECTION.md) |
| D70 | Repairs are individual operations, each run only when asked and confirmed, reporting exactly what it changed; they run as an enhance job that does only those operations and saves a new version (the source is never changed). Deleting pieces needs them named. Non-manifold edges are fixed only where duplicate or zero-area triangles cause them | Accepted (2026-11-08): reusing the job, version and comparison machinery keeps undo (switch back to the source version) and before/after views for free; operations that cannot be done without changing the shape are reported, not attempted |
| D71 | A finding is drawn in the same canvas as the model (points, edges, triangles, through the surface), and "Locate" frames its region from the current viewing direction | Accepted (2026-11-08): no second WebGL context (D67); showing or hiding a finding never moves the camera, and locating only moves it closer |
