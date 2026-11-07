# Testing on other hardware

How to check AI 3D Studio on a machine we have not tested, and how to report
it so results from different machines can be compared. Results are only claimed
for a platform after someone has run these steps on it and the numbers are in
[BENCHMARKS.md](BENCHMARKS.md).

## Status (2026-11-07)

| Platform | Status | Where the results are |
|---|---|---|
| Intel Mac (MacBook Pro 16" 2019, i7-9750H, 16 GB, Radeon Pro 5300M + UHD 630, macOS 26.7) | **Tested locally**: every suite, the large-model and rendering benchmarks on both GPUs | BENCHMARKS.md |
| Linux x86-64 (GitHub Actions `ubuntu-latest`, no GPU) | **Tested in CI** on every push: backend (SQLite, PostgreSQL, S3), frontend, Docker images, browser tests with software WebGL (SwiftShader), Rust | CI logs; no performance numbers (shared runners, software rendering) |
| Apple Silicon Mac | **Not tested** | — |
| Windows, integrated graphics | **Not tested** | — |
| Windows, dedicated GPU | **Not tested** | — |
| Linux desktop with a GPU | **Not tested** | — |

What we expect but have not verified: the backend is plain Python and NumPy
and should behave the same everywhere; the local AI engine (PyTorch) and
COLMAP/OpenMVS have their own platform builds; WebGL performance depends on the
GPU and on which GPU the browser picks (the app asks WebGL for the
high-performance GPU; on the test Mac, Chrome then draws on the Radeon, while a
plain test canvas gets the Intel GPU, which is why the benchmark reads the GPU
from the app itself).

## 1. Set up

Follow [INSTALLATION.md](INSTALLATION.md). Then record:

- CPU model, core count, memory, GPU(s) and their memory, OS version;
- `uv run python --version`, `node --version`, Chrome version
  (`chrome://version`);
- in Chrome, `chrome://gpu`: the "GL_RENDERER" line and whether WebGL2 is
  hardware accelerated.

## 2. Run the test suites

```bash
uv run ruff check . && uv run mypy && uv run pytest
pnpm lint && pnpm typecheck && pnpm test && pnpm build
pnpm exec playwright test --project=desktop
```

Report the counts printed at the end (passed, skipped, failed), and for any
failure the test name and its first error line. Skips are expected for tools
that are not installed (COLMAP, OpenMVS, Blender, the local AI engine) and for
the real-provider checks; the skip reason says which.

On Windows, run the commands in PowerShell from the repository folder; the
`.venv` paths in some browser tests assume a POSIX layout and may need
`.venv\Scripts\python.exe` (report it if a test fails for that reason).

## 3. Large-model benchmarks

The same models make results comparable:

- three synthetic terrains, made by the script: 1, 3 and 5 million triangles;
- two real scans from the [Stanford 3D Scanning Repository](http://graphics.stanford.edu/data/3Dscanrep/)
  (research use; do not redistribute): `xyzrgb_dragon.ply.gz` (7.2 million
  triangles) and `xyzrgb_statuette.ply.gz` (10 million). Download and unzip
  them yourself.

Backend (each step in its own process, peak memory included):

```bash
uv run python infrastructure/scripts/benchmark_geometry.py OUT \
  synthetic:1000000 synthetic:3000000 synthetic:5000000 \
  path/to/xyzrgb_dragon.ply path/to/xyzrgb_statuette.ply
```

Browser (uses the GLBs the backend run wrote to `OUT/*/model.glb`):

```bash
export AI3D_BENCHMARK_GLBS=OUT/synthetic-1000000/model.glb,OUT/synthetic-3000000/model.glb,OUT/synthetic-5000000/model.glb,OUT/xyzrgb_dragon/model.glb,OUT/xyzrgb_statuette/model.glb
pnpm exec playwright test e2e/benchmarks/large-models.spec.ts --project=desktop --workers=1
pnpm exec playwright test e2e/benchmarks/rendering.spec.ts --project=desktop --workers=1
# On a machine with two GPUs, once with each:
AI3D_BENCHMARK_GPU=integrated pnpm exec playwright test e2e/benchmarks/rendering.spec.ts --project=desktop --workers=1
AI3D_BENCHMARK_GPU=high-performance pnpm exec playwright test e2e/benchmarks/rendering.spec.ts --project=desktop --workers=1
```

The rendering benchmark prints one JSON line per model and reports the GPU it
used (`environment.renderer`) and whether GPU timer queries were available
(`environment.timerQueries`). Where timer queries are missing (some browsers and
drivers do not expose them), `gpuMs` is `null`: report that, do not substitute
another number.

If a model does not fit in memory, the app refuses it with "needs about N GB of
memory and M GB is free": that is a result too; report it with the numbers.

Note whether the machine was on battery or in a power-saving mode
(`pmset -g` on macOS, the power plan on Windows), and keep it awake: a run that
sleeps part-way is invalid (it happened here once: a levels build looked stuck
for 40 minutes while the lid was closed).

## 4. What to send back

```
Machine: …            OS: …            Memory: …     Power: mains/battery, power-saving on/off
GPU(s): …             Browser: Chrome …  GL_RENDERER: …
Suites: pytest N passed / N skipped / N failed; vitest …; playwright …
Backend benchmark: OUT/results.json
Rendering benchmark: the JSON lines (one per model, per GPU)
Anything that failed or was refused, with the message
```

## Known platform-specific limits

- **Apple Silicon:** the local AI engine uses PyTorch's CPU build in this setup;
  MPS acceleration has not been tried.
- **Windows:** the desktop app's sidecar and Blender path detection are covered
  by unit tests only; no Windows run yet.
- **Integrated GPUs:** shadows double the triangles drawn (a shadow pass), which
  is why they start off for models over 1.5 million triangles.
