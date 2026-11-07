# Testing on other hardware

How to check AI 3D Studio on a computer we have not tested, and how to report
it so results from different machines can be compared. A platform counts as
tested only when someone has run these steps on it and the results are recorded
in [BENCHMARKS.md](BENCHMARKS.md) with the machine report. Instructions written
for a platform are not results.

## Status (2026-11-07)

Three different things, kept apart:

| Platform | Run locally | Run in CI | Prepared, not yet run |
|---|---|---|---|
| **Intel Mac**: MacBook Pro 16" 2019, i7-9750H, 16 GB, Radeon Pro 5300M + Intel UHD 630, macOS 26.7.1 | **Yes**: every suite; geometry, smoothing, export (Blender included), rendering (both GPUs), loading and hand-over benchmarks. Low Power Mode was on | — | — |
| **Linux x86-64, no GPU** (GitHub Actions `ubuntu-latest`) | — | **Yes**, on every push: backend on SQLite, PostgreSQL and S3, frontend, browser tests with software WebGL (SwiftShader), Rust, Docker images | No performance numbers: shared runners, software rendering |
| **Apple Silicon Mac** | No | No | §A below |
| **Windows, integrated graphics** | No | No | §B below |
| **Windows, dedicated GPU** | No | No | §B below |
| **Linux desktop with a GPU** | No | No | §C below |

The release workflow (`release.yml`, Windows/Linux/macOS desktop builds) runs
only on a `v*` tag and has never run.

What we expect, unverified: the backend is Python and NumPy and should behave
the same everywhere; its memory figures should carry over within tens of
percent (the same arrays are made). The local AI engine (PyTorch), COLMAP,
OpenMVS and Blender have their own builds per platform. Rendering speed depends
entirely on the GPU and on which GPU the browser gives the page.

## 1. Every machine: set up and describe it

1. Install as in [INSTALLATION.md](INSTALLATION.md).
2. Describe the machine (read-only queries; nothing is sent anywhere):

   ```bash
   uv run python infrastructure/scripts/machine_report.py machine.json
   ```

   It records the OS, CPU, cores, memory, GPUs (with driver on Windows), power
   state (macOS Low Power Mode and power source, Windows power plan, Linux CPU
   governor), and Python, Node, pnpm and Blender versions.
3. In Chrome, open `chrome://gpu` and note `GL_RENDERER` and whether WebGL2 is
   "Hardware accelerated".
4. Plug in to mains power, switch off power saving if you can, and keep the
   computer awake (macOS: `caffeinate -i <command>`; Windows: Settings → Power,
   "Never" sleep while plugged in). A run that sleeps part-way is invalid: it
   happened here once (a levels build looked stuck for 40 minutes while the lid
   was closed).

## 2. Every machine: the test suites

```bash
uv run ruff check . && uv run mypy
uv run pytest
pnpm lint && pnpm typecheck && pnpm test && pnpm build
pnpm exec playwright test --project=desktop
pnpm test:e2e:desktop
pnpm test:e2e:workflows
```

Report the counts printed at the end (passed, skipped, failed), and for any
failure the test name and its first error line. Skips name their reason
(COLMAP, OpenMVS, Blender or the local AI engine not installed; real-provider
checks, which stay off unless you choose to run them; S3). A skip is not a pass:
list them.

## 3. Every machine: the benchmarks

The same models make results comparable: synthetic models made by the script,
and two Stanford scans (research use; download and unzip them yourself:
`xyzrgb_dragon.ply.gz`, `xyzrgb_statuette.ply.gz` from the
[Stanford 3D Scanning Repository](http://graphics.stanford.edu/data/3Dscanrep/)).

Backend (each step in its own process, its peak memory included):

```bash
uv run python infrastructure/scripts/benchmark_geometry.py OUT \
  synthetic:1000000 synthetic:3000000 synthetic:5000000 \
  path/to/xyzrgb_dragon.ply path/to/xyzrgb_statuette.ply
# Smoothing quality against a known surface (noisy cubes):
uv run python infrastructure/scripts/benchmark_geometry.py OUT --steps import,smooth_sharp,smooth_taubin \
  synthetic:1000000:cube synthetic:3000000:cube synthetic:5000000:cube synthetic:10000000:cube
```

Browser (uses the GLBs the backend run wrote):

```bash
export AI3D_BENCHMARK_GLBS=OUT/synthetic-1000000/model.glb,OUT/xyzrgb_statuette/model.glb
pnpm exec playwright test e2e/benchmarks/large-models.spec.ts --project=desktop --workers=1
pnpm exec playwright test e2e/benchmarks/rendering.spec.ts --project=desktop --workers=1
```

The rendering benchmark prints one JSON line per model, with the GPU it drew on
(`environment.renderer`, read from the app's own WebGL context) and whether GPU
timer queries were available (`environment.timerQueries`). Without timer
queries (some browsers and drivers do not expose them) `gpuMs` is `null`:
report that; do not substitute another number.

A model that does not fit in memory is refused with "needs about N GB of memory
and M GB is free". That is a result too: report it with the numbers.

## A. Apple Silicon Mac (prepared, not run)

- **Install:** the same commands; `uv` and pnpm install arm64 builds. Blender
  5.x publishes arm64 builds (the test Mac used 4.5 LTS, the last Intel build).
- **Memory:** unified memory is shared with the GPU; note the total and what
  `machine_report.py` shows as available before each run.
- **GPU:** one GPU; `AI3D_BENCHMARK_GPU` is not needed. `chrome://gpu` should
  show "ANGLE (Apple, ANGLE Metal Renderer: Apple M…)".
- **Expect to check:** the local AI engine runs on the CPU in this setup (PyTorch
  MPS has not been tried); COLMAP and OpenMVS need arm64 builds.
- **Report:** everything in §4, plus the chip (M1/M2/…, core counts) from
  `machine_report.py`.

## B. Windows (prepared, not run)

- **Install:** PowerShell from the repository folder. Some browser tests call
  `.venv/bin/python`; on Windows the path is `.venv\Scripts\python.exe`. If a
  test fails for that reason, report it (that is a bug to fix, not a skip).
- **Environment variables** in PowerShell:
  `$env:AI3D_BENCHMARK_GLBS = "OUT\xyzrgb_statuette\model.glb"`.
- **Keep awake / power:** `machine_report.py` records the active power plan;
  use "High performance" or "Balanced" on mains power and say which.
- **Integrated graphics:** run the rendering benchmark as is and check
  `environment.renderer` names the Intel/AMD integrated GPU.
- **Dedicated GPU (laptop with two GPUs):** Windows decides which GPU Chrome
  gets (Settings → System → Display → Graphics). Run once with
  `AI3D_BENCHMARK_GPU=integrated` and once with `high-performance`, and check
  `environment.renderer` each time: on Windows Chrome may ignore its switches
  and use the GPU Windows assigns, so the renderer line, not the setting, says
  which GPU was measured.
- **Desktop app:** the sidecar and Blender path detection are covered by unit
  tests only; `pnpm test:e2e:desktop` drives the desktop UI in a browser, not
  the Tauri window.

## C. Linux desktop with a GPU (prepared, not run)

- **GPU in the browser:** `chrome://gpu` must show hardware acceleration; on
  some distributions Chrome blocks GPUs it does not recognise, which falls back
  to software rendering (SwiftShader, as in CI). Report the renderer line.
- **Two GPUs (laptops):** start the run with the dedicated GPU selected
  (`DRI_PRIME=1` for Mesa drivers, `prime-run` or
  `__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia` for NVIDIA) and
  check `environment.renderer`.
- **COLMAP with CUDA:** the dense COLMAP path needs CUDA and has never been run;
  report it separately if you try it.

## 4. What to send back

```
machine.json (from machine_report.py)
Chrome version and GL_RENDERER (chrome://gpu)
Power: mains or battery, power-saving on or off, kept awake: yes/no
Suites: pytest N passed / N skipped / N failed; vitest …; playwright …; with each failure's first error line
Backend benchmark: OUT/results*.json
Rendering benchmark: the JSON lines (one per model, per GPU)
Anything refused or failed, with the message
```

## Known platform limits

- **Apple Silicon:** PyTorch MPS acceleration has not been tried for the local
  AI engine.
- **Windows:** no run of any kind yet; the desktop sidecar and Blender path
  detection are unit-tested only.
- **Integrated GPUs:** shadows draw the model twice (a shadow pass), which is
  why they start off for models over 1.5 million triangles.
