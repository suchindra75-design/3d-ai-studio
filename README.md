# AI 3D Studio

**Open-source, local-first image-to-3D reconstruction.** Turn one photograph
or a full photo set of a real object into an inspectable, exportable 3D model
— on your own machine, with the AI providers you choose.

> **Status: release candidate for 0.1.0 — all seven roadmap phases are
> implemented; nothing has been released yet.** Projects, photo upload,
> quality analysis, the capture assistant, hardware detection, the AI
> generation pipeline (jobs, live progress, consent, Tripo and Meshy adapters)
> the 3D workspace (viewer, inspection, transforms, materials, undo/redo,
> versions) and local photogrammetry with COLMAP (measured, textured, on your
> computer) work today, and so does **turning one photo into a 3D model on your
> own computer** with the optional local AI engine (TripoSR). The Tripo and Meshy integrations have **not yet been
> run against the real services**, which need a paid key. Enhancing models,
> verified exports and the desktop app (with its own bundled local engine)
> work too. Desktop builds are unsigned, and only the macOS Intel build has
> been made and checked so far — see the
> [roadmap](docs/IMPLEMENTATION_ROADMAP.md). Nothing in the app
> pretends otherwise.

## What it does (and will do)

|            | Today                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | Planned                                                                               |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| Projects   | create, search, archive, delete; import GLB/OBJ/STL/PLY/3MF (and ZIPs) as models; back up to one ZIP and restore                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | FBX import, asset library                                                             |
| Photos     | JPEG/PNG/WebP upload, originals preserved, dedupe, EXIF (GPS never exposed), thumbnails                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | preprocessing & segmentation                                                          |
| Quality    | per-photo sharpness/exposure/clipping analysis; set-level capture check; overlap measured by feature matching                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |                                                                                       |
| Generation | Local photogrammetry (COLMAP 4 + optional OpenMVS for dense surfaces on the CPU: measured, textured, nothing uploaded); local AI from one photo (TripoSR, optional install, runs on a CPU); Tripo & Meshy (cloud, your key, per-job consent; untested with real keys); live progress, cancel/retry                                                                                                                                                                                                                                                                                         | dense surfaces on CUDA (implemented, untested), larger local AI models on NVIDIA GPUs |
| Workspace  | Three.js viewer (normals/UV views, lighting presets, section, measuring, screenshots, version comparison), inspection, move/rotate/scale, delete/mirror/pivot, ground/centre, plane cuts with filled caps, texture replace and UV view, materials, undo/redo, versions; Enhance (clean-up, normals, fragments, ground removal, real-world scale, smoothing, QuadriFlow/voxel retopology with a fidelity check, normal and occlusion maps); AI texture upscaling on this computer (Real-ESRGAN); re-texturing by Meshy/Tripo with consent; full quality report; texture baking with Blender | vertex-level editing, quads in stored files                                           |
| Export     | GLB, glTF, OBJ+MTL, STL, 3MF, USDZ, FBX (with Blender) — each re-opened, colours included; STL/3MF Z-up for slicers — and compared with the model before it is offered                                                                                                                                                                                                                                                                                                                                                                                                                     |                                                                                       |
| Platforms  | web app, desktop app (Tauri) with its own bundled engine, Docker; optional S3 storage                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | signed desktop builds                                                                 |

**Honesty rules:** single-photo results invent the hidden sides of the object;
AI meshes are not dimensionally accurate; photogrammetry has an unknown scale
without a reference. The app tells you which kind of result you have.

## Quick start

Requirements: Node ≥ 22 with Corepack, [uv](https://docs.astral.sh/uv/),
Python 3.11/3.12 (uv can install it). Rust only for the desktop app.

```bash
corepack enable pnpm
pnpm install
uv sync --all-packages
cp .env.example .env        # keeps development data in ./.data
pnpm dev:backend            # http://127.0.0.1:8000  (API docs at /api/docs)
pnpm dev:web                # http://localhost:3000
pnpm dev:desktop            # optional: native window with its own engine (needs Rust)
```

Or self-host with Docker: `docker compose up --build` → <http://localhost:3000>.

Full instructions: [docs/INSTALLATION.md](docs/INSTALLATION.md).

## Architecture

```
apps/web (Next.js) ─┐                       ┌─ backend/ai3d (FastAPI, SQLAlchemy, Alembic)
apps/desktop (Tauri)┴─ packages/studio ── api-client ── /api/v1 ─┤   storage · processing · (providers, pipelines, workers)
                      packages/ui · shared-types (generated)      └─ local-engine/ai3d_engine (hardware, COLMAP/Blender runners)
```

SQLite by default (PostgreSQL supported and tested), local file storage, a
DB-backed job runner that survives restarts. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Documentation

| Document                                               |                                                        |
| ------------------------------------------------------ | ------------------------------------------------------ |
| [Product specification](docs/PRODUCT_SPECIFICATION.md) | scope, quality & processing modes, principles          |
| [Architecture](docs/ARCHITECTURE.md)                   | structure, decisions, security model                   |
| [AI pipeline](docs/AI_PIPELINE.md)                     | provider interface, pipelines, routing, model licences |
| [Database schema](docs/DATABASE_SCHEMA.md)             | tables and conventions                                 |
| [Roadmap](docs/IMPLEMENTATION_ROADMAP.md)              | phases and what is done                                |
| [Installation & development](docs/INSTALLATION.md)     | setup, run, build, Docker                              |
| [AI providers](docs/AI_PROVIDERS.md)                   | configuration, **costs**, output licensing             |
| [Local processing & GPUs](docs/LOCAL_GPU_SETUP.md)     | COLMAP, Blender, CUDA, platform support                |
| [Testing](docs/TESTING.md)                             | suites, and where each workflow is tested              |
| [Releasing](docs/RELEASING.md)                         | desktop builds, bundled engine, signing, licences      |
| [Error reporting](docs/ERROR_REPORTING.md)             | optional, off by default; exactly what is sent         |
| [Troubleshooting](docs/TROUBLESHOOTING.md)             | common problems                                        |
| [Contributing guide](docs/CONTRIBUTING_GUIDE.md)       | rules, checks, how-tos                                 |

## Costs and licences

AI 3D Studio is free software under the [Apache License 2.0](LICENSE) and
contains no billing or telemetry. **External AI providers are paid services**
(Tripo: from ~US$0.20–0.30 per generation; Meshy: API requires a paid plan) with
their own terms and output licences — see [AI_PROVIDERS.md](docs/AI_PROVIDERS.md).
No model weights are bundled; local models you install keep their own licences,
some of which restrict commercial use or regions.

## Hardware

Upload and analysis run anywhere. Local AI generation from one photo (TripoSR)
runs on the CPU with 8 GB of RAM or more (about 1.5 minutes per photo on a
6-core Intel Mac; an NVIDIA GPU is used when present). Dense photogrammetry
needs an NVIDIA GPU with CUDA. The Settings screen reports what your machine
supports.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), [Code of Conduct](CODE_OF_CONDUCT.md)
and [Security policy](SECURITY.md).
