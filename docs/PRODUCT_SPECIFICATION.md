# AI 3D Studio — Product Specification

Status: living document · Applies from Phase 0 onward · License: Apache-2.0

## 1. Purpose

AI 3D Studio is an open-source, local-first platform that turns photographs of
real objects into inspectable, exportable 3D models. It combines two
fundamentally different techniques behind one project and asset system:

| Technique | Input | How geometry is obtained | What it can honestly claim |
|---|---|---|---|
| **AI generation** (single image) | 1 photo | A generative model *infers* a plausible shape and texture | Visible surfaces follow the photo; **unseen surfaces are invented** |
| **AI generation** (multi-view) | 2–8 photos of the same object | Generative model conditioned on several views | Better coverage, still inferred, not measured |
| **Photogrammetry** (multi-image) | ~20–200 overlapping photos | Feature matching → camera poses → dense stereo → surface reconstruction (COLMAP / Open3D) | Geometry is *measured* up to an unknown global scale |

The product never presents inferred geometry as measured geometry, and never
claims a model is CAD-ready or dimensionally accurate.

## 2. Principles

1. **Local-first.** Projects, images and models live on the user's disk. The
   default install needs no account and no network.
2. **Explicit cloud.** Images are only sent to a cloud provider when the user
   has configured that provider *and* the project/job allows it. Cloud
   fallback from a local job always requires explicit consent.
3. **Interchangeable engines.** Every generation/reconstruction backend sits
   behind a provider interface with discoverable capabilities. No provider is
   assumed to support another provider's options.
4. **Non-destructive.** Original uploads are stored byte-for-byte. Every
   processing step (enhance, cleanup, retopology, export) creates a new model
   version; nothing overwrites an earlier version.
5. **Honest UI.** No decorative buttons, no simulated progress, no mock
   results shown as real output. Features that are not implemented are not
   shown as if they were.
6. **No monetization machinery.** No subscriptions, credits, billing or
   payment code. External provider costs are documented and shown to the user
   where the provider reports them.

## 3. Users and primary scenarios

| Persona | Scenario |
|---|---|
| Hobbyist / maker | Snap one photo of an object, get a quick GLB preview, export STL for a 3D print (with a clear warning that the back side is inferred and scale is approximate). |
| 3D artist / game dev | Generate a textured PBR asset in High Quality mode, inspect topology, re-run Professional processing, export GLB/FBX. |
| Product photographer | Shoot 40–80 photos on a turntable, run photogrammetry locally, get a measured mesh with a quality report. |
| Self-hoster / researcher | Run the backend in Docker on a GPU box, add a new local model as a provider plugin. |

## 4. Functional scope

### 4.1 Projects and inputs
- Create, rename, describe, archive and delete projects.
- Project **input mode**: `single_image` or `multi_view`.
- Upload JPEG, PNG and WebP (validated by content, not extension). Originals
  are preserved; thumbnails and analysis results are derived.
- Per-image quality analysis on upload: resolution, sharpness (variance of
  Laplacian), exposure, clipping, EXIF camera metadata.
- Duplicate detection by SHA-256 within a project (idempotent re-upload).
- **Capture assistant** (multi-view): a shooting plan plus set-level checks —
  image count, blur, lighting consistency, resolution/focal-length
  consistency, and later (Phase 4) feature-overlap assessment. Warnings cover
  insufficient overlap, blur, changing lighting, reflective/transparent
  surfaces, moving objects and missing views.

### 4.2 Generation and reconstruction
- Quality modes: **Fast**, **High Quality**, **Professional** (see §5).
- Processing modes: **Cloud**, **Local**, **Automatic** (see §6).
- Jobs are persistent, observable (SSE with polling fallback), cancellable and
  retryable; they survive application restarts.
- Single-image pipeline and multi-image pipeline are separate pipelines (see
  `AI_PIPELINE.md`). Photogrammetry is never run on a single photo.
- **Enhance existing model**: re-process an existing version into a new,
  higher-quality version; the source version is preserved.

### 4.3 Workspace and inspection
- Three.js / React Three Fiber viewer: GLB/GLTF, orbit/pan/zoom, camera
  presets, environment lighting, shadows, grid/axes, wireframe/solid/material
  views, hierarchy, selection, bounding box, dimensions, vertex/triangle
  counts.
- Progressive editing: transform, material/colour, texture settings, mesh
  selection, optimization, undo/redo, version history. Vertex-level editing is
  not offered until it genuinely exists.

### 4.4 Quality reports
Per model version: triangle/vertex counts, connected components, watertight,
manifold (edge-manifold), winding consistency, degenerate/duplicate faces,
non-finite values, UV presence and overlap estimate, texture resolution,
bounding box and scale status, and an explicit **provenance** section stating
whether surfaces are measured or inferred.

### 4.5 Export
GLB, GLTF, OBJ (+MTL/textures), STL (geometry only), FBX, USDZ, 3MF. A
format is only offered when its exporter is installed and validated on that
machine. Every export is re-opened and validated before it is marked ready.

## 5. Quality modes

Quality modes are backend **profiles**, translated per provider into whatever
parameters that provider actually supports.

| Aspect | Fast | High Quality | Professional |
|---|---|---|---|
| Goal | Speed, previews | Visual realism | Production usability |
| Provider settings | Provider's fastest model/tier | Best model, PBR, higher texture res | Best model + post-processing |
| Mesh cleanup | Validate, remove NaNs/degenerates | + merge vertices, remove small components, fix normals | + retopology/decimation where available, UV checks, texture baking |
| Validation | Basic integrity | Integrity + normals | Full report: manifold, watertight, UVs, scale |
| Report | Summary | Summary | Detailed report |

## 6. Processing modes

| Mode | Behaviour |
|---|---|
| Cloud | Use configured cloud providers. Requires the project to allow cloud upload. |
| Local | Use only local CPU/GPU engines. Never contacts external providers. |
| Automatic | Choose a route from input type, quality mode, detected hardware and provider capabilities. If the best route is cloud and the project has not allowed cloud upload, the job asks for consent instead of uploading. |

## 7. Non-functional requirements

- **Platforms:** macOS (Apple Silicon and Intel), Windows 10+, Linux (x86_64).
  CUDA-only tools are never assumed on macOS.
- **Security:** API keys only server-side (env or encrypted settings), never
  in browser bundles, logs or exported projects. The local agent exposes no
  arbitrary command execution.
- **Reliability:** SQLite (WAL) by default; PostgreSQL optional. Jobs are
  stored in the database with leases so they recover after a crash.
- **Accessibility:** keyboard navigation, visible focus, ARIA labels, reduced
  motion support, sufficient contrast in the dark theme.
- **Responsiveness:** desktop-class workspace ≥1024px; mobile web supports
  upload, job progress and model inspection.

## 8. Out of scope (for now)
Accounts and multi-user permissions, billing of any kind, collaborative
editing, CAD/parametric modelling, vertex sculpting, scanning hardware
integrations.

## 9. Known, permanent limitations (must be stated in the UI)
- Single-image results invent the back and hidden sides of the object.
- AI-generated meshes are not dimensionally accurate; photogrammetry has an
  unknown global scale unless a reference is provided.
- Reflective, transparent and texture-less surfaces reconstruct poorly with
  photogrammetry.
- Cloud providers have their own pricing, rate limits and output licensing;
  AI 3D Studio does not control them.
