# Installation & local development

## Requirements

| Tool | Version | Notes |
|---|---|---|
| Node.js | ≥ 22 (24 recommended) | `.nvmrc` pins 24 |
| pnpm | 12 (via Corepack) | `corepack enable pnpm` |
| Python | 3.11 or 3.12 | uv installs it if missing |
| uv | ≥ 0.13 | <https://docs.astral.sh/uv/> |
| Rust | stable (≥ 1.77.2) | only for the desktop app — <https://rustup.rs> |
| Docker | optional | for container deployment / PostgreSQL tests |

Platform prerequisites for the desktop app follow Tauri's guide
(<https://tauri.app/start/prerequisites/>): Xcode Command Line Tools on macOS,
WebView2 + MSVC build tools on Windows, `libwebkit2gtk-4.1-dev` and friends on
Linux.

Python is capped at 3.12 because Open3D/PyTorch wheels lag new Python
releases (see `docs/ARCHITECTURE.md` D7).

## First-time setup

```bash
corepack enable pnpm
pnpm install
uv sync --all-packages        # creates .venv with backend + local engine + dev tools
                              # (--all-extras adds Open3D for photogrammetry point filtering)
cp .env.example .env          # optional; sets AI3D_DATA_DIR=.data for development
```

## Run

Three terminals (or your IDE's run configurations):

```bash
pnpm dev:backend     # FastAPI on http://127.0.0.1:8000 (API docs: /api/docs)
pnpm dev:web         # Next.js on http://localhost:3000 (proxies /api/v1 to the backend)
pnpm dev:desktop     # Tauri window (starts its own Vite server on :1420)
```

The desktop app starts its **own private engine** — you do not need
`pnpm dev:backend` for it. Development builds run the repository's
`.venv/bin/ai3d-server` (so run `uv sync --all-packages` first), from the
repository so `.env` applies, but with the app's own data folder
(macOS: `~/Library/Application Support/org.ai3dstudio.desktop`; Settings →
Data folder → *Open*). The engine listens on a random `127.0.0.1` port and
only answers the window that started it. Its log is `logs/backend.log` in that
folder.

Release builds (`pnpm build:desktop-app`) carry their own engine (a bundled
Python with the backend; see [RELEASING.md](RELEASING.md)) and run it from the
app's data folder, so a `.env` file **in the data folder** configures it (for
example `AI3D_COLMAP_PATH=…`). `AI3D_DESKTOP_BACKEND=/path/to/ai3d-server`
overrides the engine for any build.

Data goes to `AI3D_DATA_DIR` (`.data/` with the example `.env`; otherwise the
OS user-data folder, e.g. `~/Library/Application Support/AI3DStudio`). The
SQLite database is migrated automatically on start-up.

## Build

```bash
pnpm build                  # Next.js production build + desktop frontend (Vite)
pnpm bundle:engine          # the desktop app's engine (CPython + backend), with a smoke test
pnpm build:desktop-app      # bundle:engine + `tauri build` → .app/.dmg, .msi/.exe, .deb/.AppImage
pnpm --filter @ai3d/web start   # serve the production web build
```

## Docker (self-hosting)

Needs Docker Compose 2.20 or newer. Tested with Docker Engine 29.8 and
Compose 5.5 (Docker Desktop 4.92, linux/amd64).

```bash
docker compose up --build        # web on http://localhost:3000, API on :8000
docker compose down              # stop; data stays in the volume
```

- Open <http://localhost:3000>. The web container proxies `/api/v1` to the
  backend container, so the browser only talks to port 3000; port 8000 is
  published for API clients and `curl http://127.0.0.1:8000/api/v1/health`.
- Data (SQLite database, photos, models, the generated `secret.key`) persists
  in the `ai3d-data` volume (`/data` in the backend container).
  `docker compose down -v` deletes it.
- Ports are bound to `127.0.0.1`. The app has no login: anyone who can reach
  port 3000 can use and delete every project, so publish it on a LAN address
  only deliberately. Through the web port no further change is needed (the
  proxy calls the backend as `backend:8000`); if you also publish port 8000,
  add your host name to `AI3D_ALLOWED_HOSTS` (and to `AI3D_CORS_ORIGINS` if
  browsers call the API directly). To use other host ports, add a
  `docker-compose.override.yml` with
  `ports: !override ['127.0.0.1:3001:3000']` for the service (Compose 2.24+).
- The web image bakes the backend URL at build time (`AI3D_API_URL` build arg,
  default `http://backend:8000`).
- Compose substitutes `AI3D_DATABASE_URL`, `AI3D_SECRET_KEY`,
  `AI3D_TRIPO_API_KEY`, `AI3D_MESHY_API_KEY` and `AI3D_POSTGRES_PASSWORD` from
  your shell **and from `./.env`**. A development `AI3D_DATABASE_URL` pointing
  at `localhost` in `.env` will not work inside the container.
- Both containers run as non-root users with a read-only root filesystem,
  `/tmp` as tmpfs, all Linux capabilities dropped and `no-new-privileges`.
- PostgreSQL instead of SQLite (the password is required; there is no default):

  ```bash
  export AI3D_POSTGRES_PASSWORD="$(openssl rand -hex 16)"   # keep it, e.g. in .env
  export AI3D_DATABASE_URL="postgresql+psycopg://ai3d:${AI3D_POSTGRES_PASSWORD}@postgres:5432/ai3d"
  docker compose --profile postgres up --build
  ```

  The backend waits for PostgreSQL to be healthy and applies the migrations on
  start. Photos and models still live in the `ai3d-data` volume; the database
  is in `ai3d-postgres`. PostgreSQL is not published on a host port.
- Local photogrammetry in the container: build the COLMAP variant (below) with
  `AI3D_BACKEND_TARGET=backend-colmap docker compose up --build`.

### Container images

| Image | Build | Compressed (pull) | On disk¹ | Contains |
|---|---|---|---|---|
| backend (default) | `docker build -f infrastructure/docker/backend.Dockerfile -t ai3d-backend .` | 259 MB | 1.19 GB | Python 3.11 slim, API, PostgreSQL driver, USDZ export (Pixar `usd-core`) |
| backend + COLMAP | same, plus `--target backend-colmap` | 414 MB | 1.78 GB | the above + CPU COLMAP 4.2.1 (conda-forge), `AI3D_COLMAP_PATH` set |
| web | `docker build -f infrastructure/docker/web.Dockerfile -t ai3d-web .` | 94 MB | 391 MB | Next.js standalone server on Node 24 slim |

¹ As reported by `docker image ls` on Docker Desktop 4.92 (linux/amd64,
measured 2026-11-04); "on disk" includes the unpacked layers.

- **Build arguments (backend):** `AI3D_EXTRAS` (default `"postgres usdz"`)
  selects the optional Python extras. Without `usdz` the image is 30 MB smaller
  compressed (229 MB; 0.94 GB on disk) and USDZ is listed as unavailable. Adding
  `reconstruction` installs Open3D for point-cloud filtering (large; not needed
  for photogrammetry to run). `COLMAP_SPEC` (default `colmap=4.2.1=cpu*`)
  selects the conda-forge COLMAP build.
- **USDZ:** `usd-core` is licensed under the Tomorrow Open Source Technology
  License 1.0; its licence text ships in the image under
  `/app/.venv/lib/python3.11/site-packages/usd_core-*.dist-info/licenses/`.
- **COLMAP variant:** Debian's `colmap` package is 3.x, which the app does not
  support, so the image takes COLMAP 4 from conda-forge and keeps only the
  executable and the libraries it links (≈ 420 MB instead of the 1.4 GB
  environment). It is a CPU build: camera poses, sparse points and a coarse
  textured surface; no dense stereo (needs CUDA). `colmap -h` works in the
  container, `GET /api/v1/devices/local` reports COLMAP 4.2.1 as installed and
  the *Local photogrammetry (COLMAP)* engine is offered.
- **Not in any image:** Blender (FBX export, texture baking) and GPU support.
- The backend image contains no credentials. Provider keys and
  `AI3D_SECRET_KEY` are passed as environment variables only.

## Optional backends

**S3-compatible storage** instead of the data folder (AWS S3, Ceph, Cloudflare
R2, SeaweedFS, …). The database is still SQLite or PostgreSQL:

```bash
uv sync --all-packages --extra s3
AI3D_STORAGE_BACKEND=s3 AI3D_S3_BUCKET=ai3d AI3D_S3_ENDPOINT_URL=https://s3.example.org \
  AI3D_S3_ACCESS_KEY_ID=… AI3D_S3_SECRET_ACCESS_KEY=… ai3d-server
```

The bucket can stay private: downloads are streamed through the backend.
Existing data in the data folder is not moved automatically.

**Error reporting** to a Sentry-compatible service you choose. It is off by
default and sends errors only, scrubbed; see [ERROR_REPORTING.md](ERROR_REPORTING.md).

## Tests

All suites and what each workflow is tested by: [TESTING.md](TESTING.md). The
pre-PR checklist is in [CONTRIBUTING_GUIDE.md](CONTRIBUTING_GUIDE.md#checks-run-before-every-pr).
Playwright uses your installed Google Chrome locally (`channel: 'chrome'`) and
Playwright's Chromium in CI (`pnpm exec playwright install chromium`).

## Optional: generation tools

- Cloud providers: [AI_PROVIDERS.md](AI_PROVIDERS.md)
- COLMAP, Blender, GPU set-up: [LOCAL_GPU_SETUP.md](LOCAL_GPU_SETUP.md)
- Problems: [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
