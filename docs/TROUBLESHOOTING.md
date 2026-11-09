# Troubleshooting

## "Backend not reachable" / status shows *Backend offline*

- Start it: `pnpm dev:backend` (from the repository root).
- Web app: the Next.js server proxies `/api/v1` to `AI3D_API_URL`
  (default `http://127.0.0.1:8000`). Restart `pnpm dev:web` after changing it.
- Desktop app: it starts its own engine; see *Desktop app: "The local engine
  could not start"* below.
- Check `curl http://127.0.0.1:8000/api/v1/health`.

## Port already in use

`ai3d-server --port 8001` / `pnpm --filter @ai3d/web exec next dev --port 3001`.
The desktop dev server requires port 1420 (`strictPort`).

## Upload rejected

| Error code | Meaning |
|---|---|
| `unsupported_media_type` | Not JPEG/PNG/WebP (checked by content), or animated. iPhone HEIC: most browsers convert to JPEG when picking from Photos; otherwise export as JPEG. |
| `payload_too_large` | Over `AI3D_MAX_UPLOAD_BYTES` (50 MB) or `AI3D_MAX_IMAGE_PIXELS` (150 MP). |
| `corrupt_image` | File is truncated or damaged. |
| `limit_exceeded` | Project already has `AI3D_MAX_IMAGES_PER_PROJECT` photos. |
| `duplicate` (status) | The same bytes are already in the project; nothing was added. |

## Photo flagged as blurry / soft

Sharpness is the variance of the Laplacian on a ≤1024 px copy. Very smooth or
dark subjects can score low even when in focus — the warning is a hint, not a
verdict. For photogrammetry, retake genuinely blurry photos.

## Next.js dev page never finishes loading when opened via 127.0.0.1

Next.js blocks dev-only assets for unknown origins. `127.0.0.1` is allowed in
`apps/web/next.config.ts`; for other hostnames add them to `allowedDevOrigins`
or use `http://localhost:3000`.

## `uv` / `pnpm` not found

- pnpm: `corepack enable pnpm`.
- uv: install from <https://docs.astral.sh/uv/> and make sure `~/.local/bin`
  is on `PATH` (open a new terminal after installing).

## Desktop app: "The local engine could not start"

The start-up screen shows the reason and the last lines of the engine's log
(full log: `logs/backend.log` in the app's data folder).

- *not included in this build*: release builds do not bundle the engine yet.
  Run `uv sync --all-packages` in the repository and use `pnpm dev:desktop`,
  or set `AI3D_DESKTOP_BACKEND` to an `ai3d-server` executable.
- *exited during start-up*: read the log; usually a broken `.venv` (run
  `uv sync --all-packages --all-extras`) or a database migration error.
- *did not answer within 90 s*: a very slow first start (Python byte-code
  compilation) or a blocked loopback interface; press *Try again*.

## Desktop app: an engine is still running after the app closed

The engine stops itself within a few seconds of the app disappearing
(`AI3D_PARENT_PID` watchdog). If one is left over from an older build:
`pkill -f ai3d-server` (macOS/Linux).

## `401 unauthorized` from the API

The backend was started with `AI3D_API_TOKEN` (the desktop app does this) and
the request had no or the wrong token. Send `Authorization: Bearer <token>`.
The desktop app's token changes on every launch and is not shown anywhere;
use `pnpm dev:backend` for a backend you can call by hand.

## Desktop build fails

- Rust missing: install via <https://rustup.rs>, then open a new terminal.
- Linux: install `libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf`.
- First build compiles ~400 crates (several minutes); later builds are incremental.
- macOS, `bundle_dmg.sh` fails with *"Finder got an error: AppleEvent timed
  out"*: the DMG step asks Finder to lay out the window and needs Automation
  permission for your terminal. Grant it in System Settings → Privacy &
  Security → Automation, or build with `CI=true pnpm build:desktop-app` (plain
  DMG window).
- `pnpm bundle:engine` fails in `uv pip install --require-hashes`: `uv.lock` is
  out of date; run `uv lock` and commit the result.

## Desktop app: first launch takes a long time

The first launch after installing can take up to a minute while macOS checks
the engine's libraries; later launches take about 10 seconds. The start-up
screen stays up until the engine answers.

## Desktop app (release build) cannot find COLMAP

Apps started from Finder do not see your shell's `PATH`. The engine also
looks in `/opt/homebrew/bin`, `/usr/local/bin`, `/Applications/COLMAP.app` and
`~/.local/share/ai3d-studio/tools/colmap/bin` (the install from
[LOCAL_GPU_SETUP.md](LOCAL_GPU_SETUP.md)). Anywhere else: put
`AI3D_COLMAP_PATH=/path/to/colmap` in a `.env` file in the app's data folder
(Settings → Data folder → *Open*) and restart the app.

## Local AI engine (TripoSR)

- *"not installed"* in the provider list or plan: run
  `uv run ai3d-local-ai install` (needs uv and ~4 GB free), or set
  `AI3D_LOCAL_AI_DIR` to where you installed it, then restart the backend.
- `ai3d-local-ai status` says *incomplete* or *from another version*: run the
  install again; finished downloads are reused when their checksum matches.
- *"No object was found in the photo"*: the background removal kept nothing.
  Use a photo with one clearly visible object that fills a good part of the
  frame, or a PNG whose background is already transparent.
- Parts are missing from the model (ears, handles, legs) or it is cut flat on
  one side: press **Check cut-out** in the capture check and look at "What the
  AI will see" in the photo's details. Leave space around the object, use a
  plain background that contrasts with it, or upload a PNG with the background
  already removed (its transparency is used as it is).
- The job log says *"the photo was not projected"* (*"outline did not line up"*):
  the model's outline overlapped the cut-out less than 60%, usually because the
  cut-out kept background or lost large parts. The model is still textured with
  TripoSR's own colours; check the cut-out and try a cleaner photo.
- *"Refining the model did not work … it is kept as TripoSR made it"*: the model
  is TripoSR's raw output (dense, colours per vertex). Please report the job's
  log; generating again usually gives the same result.
- The first generation after installing is slow (several minutes) while the
  operating system checks the new libraries; later runs take about 1.5
  minutes on a 6-core CPU.
- The engine's log for a failed job is in the job's error details
  (`log_tail`).
- *"needs a computer with at least 8.0 GB of memory"*: local AI is not
  offered on this computer. Automatic mode uses a cloud service instead (it
  asks first); Local mode cannot run single photos.
- *"needs about … of free disk space"*: free up space on the disk that holds
  the data folder. Photogrammetry needs about three times the size of the
  photos, at least 2 GB.
- *"Waiting for another local job on this computer to finish"*: only one heavy
  job (photogrammetry, local AI or Blender baking) runs at a time; it starts
  by itself.
- A failed generation may show **Try another engine**. A cloud choice says
  what is sent and to whom, and nothing is uploaded until you confirm.

## S3 storage errors

- *"AI3D_STORAGE_BACKEND=s3 needs the s3 extra"*: `uv sync --extra s3` (the
  Docker image does not include it; add it with
  `--build-arg AI3D_EXTRAS="postgres usdz s3"`).
- `GET /api/v1/health` reports `"storage": "unavailable"`: the bucket does not exist or the
  credentials are wrong (`SignatureDoesNotMatch`, `InvalidAccessKeyId`).
  The backend never creates buckets.

## Database

- Reset local data: stop the backend and delete the data folder shown in
  Settings (this deletes all projects).
- Migration errors after pulling changes: `cd backend && uv run alembic upgrade head`
  and check the output. Never edit an applied migration; add a new one.

## Hardware detection says something is missing

Click **Re-detect** in Settings after installing tools (results are cached
for 60 s). Detection only runs fixed, allow-listed commands (`nvidia-smi`,
`system_profiler`, `colmap`, `blender`, …); a tool must be on `PATH` or in
its standard install location.

## Local photogrammetry

- **"COLMAP was not found" / "COLMAP 3.x is installed"**: install COLMAP 4
  ([LOCAL_GPU_SETUP.md](LOCAL_GPU_SETUP.md#colmap-photogrammetry)) and set
  `AI3D_COLMAP_PATH`, then restart the backend.
- **"COLMAP could not find two photos that overlap enough to start"** or only a
  few photos placed: run *Measure overlap* in the capture check. Photos listed
  as isolated share too little with the rest — retake them closer to their
  neighbours (60–80% overlap), keep the object still, avoid plain or shiny
  surfaces (a patterned mat underneath helps).
- **The model is coarse or has holes**: expected without an NVIDIA GPU — the
  surface is built from sparse points. More photos (40+) help.
- **The size looks wrong**: photogrammetry recovers shape, not size; the
  numbers are in the model's own units until a reference scale exists.


## Docker

- **`docker compose up` says a port is already allocated**: something (often
  `pnpm dev`) already uses 3000 or 8000. Stop it, or map other host ports with
  a `docker-compose.override.yml` (see
  [INSTALLATION.md](INSTALLATION.md#docker-self-hosting)).
- **`Cannot connect to the Docker daemon` / `500 Internal Server Error` from
  `docker info` with Docker Desktop running**: the Docker Desktop VM is not up
  (this can happen after an update was applied while it was running). Run
  `docker desktop restart`, or quit and reopen Docker Desktop.
- **Backend keeps restarting with a database error**: check
  `docker compose logs backend`. A development `AI3D_DATABASE_URL` in `./.env`
  (e.g. `…@localhost:5432/…`) is passed into the container, where `localhost`
  is the container itself; unset it or point it at `postgres:5432` with the
  `postgres` profile.
- **`postgres` exits with "superuser password is not specified"**: set
  `AI3D_POSTGRES_PASSWORD` (and the matching `AI3D_DATABASE_URL`). PostgreSQL
  stores the password when its volume is first created; changing it later
  needs `ALTER USER` or a fresh `ai3d-postgres` volume.
- **"Invalid host header" (400) on port 8000**: add the host name you use to
  `AI3D_ALLOWED_HOSTS` in `docker-compose.yml`. Requests through the web app
  on port 3000 are not affected.
- **Local photogrammetry says COLMAP was not found**: the default image has
  no COLMAP. Rebuild with
  `AI3D_BACKEND_TARGET=backend-colmap docker compose up --build`.
