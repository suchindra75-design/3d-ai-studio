# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased] — 0.1.0

First release candidate. Nothing has been published yet.

### Added

- **Projects and photos.** Create, search, archive and delete projects.
  Upload JPEG, PNG and WebP photos; originals are kept and duplicates are
  detected. Each photo gets a sharpness, exposure and clipping analysis, and
  the capture check measures overlap between photos.
- **AI generation.**
  - Pipeline: jobs that survive restarts, live progress, cancel and retry,
    explicit consent before anything is uploaded, and cost estimates.
  - Tripo and Meshy adapters. **Not yet run against the real services**,
    which need a paid key.
- **Local photogrammetry with COLMAP 4.** Measured, textured models built on
  your computer; nothing is uploaded. The dense (CUDA) path is implemented
  but not tested.
- **3D workspace.** Viewer, inspection, move/rotate/scale, materials,
  undo/redo, and version history.
- **Enhance.**
  - Clean-up, normals, loose fragments, ground removal, real-world scale.
  - Texture baking with Blender.
  - Full quality report.
- **Export.** GLB, glTF, OBJ+MTL, STL and 3MF (millimetres), USDZ, and FBX
  (with Blender). Each file is re-opened and compared with the model before
  it is offered.
- **Desktop app (Tauri).**
  - Runs its own private engine on a random local port with a per-launch
    token.
  - Restarts the engine if it crashes, stops it cleanly on Quit, and the
    engine stops itself if the app disappears.
  - Uses the native Save dialog.
  - Release builds bundle the engine (CPython and the backend) with a licence
    list.
- **Self-hosting.** Docker images and Compose, with optional PostgreSQL.
- **Optional S3-compatible storage** (`AI3D_STORAGE_BACKEND=s3`).
- **Optional error reporting** to a Sentry-compatible service you choose:
  errors only, scrubbed, off by default.
- **Release workflow.** Desktop builds for macOS, Linux and Windows, and
  container images.

### Known limitations

See [the roadmap](docs/IMPLEMENTATION_ROADMAP.md#known-gaps-carried-forward).
