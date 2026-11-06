# Capability audit (2026-11-04, updated after Phases 8–15)

An audit of the repository after phases 0–7 (commit `f8ec4af`), against the
product vision in [PRODUCT_SPECIFICATION.md](PRODUCT_SPECIFICATION.md).
"Verified" means exercised by automated tests or by hand **with real tools**
(COLMAP, Blender, Pixar USD, a real browser). The only stand-ins are the
test-only mock provider and the fake Tripo/Meshy HTTP servers.

Legend: ✅ fully implemented and verified · 🟡 implemented but not verified
with real-world data · 🟠 partially implemented · ❌ not implemented

| # | Area | Status | One-line reason |
|---|---|---|---|
| 1 | Single-image → 3D | 🟡 | Local path verified on a real photo (Phase 8); Tripo/Meshy still never called for real |
| 2 | Multi-image → 3D | ✅ | With OpenMVS (Phase 9b): a real 67-photo capture became a detailed, upright, textured model on the CPU in ~7 min; without OpenMVS the sparse surface is coarse; CUDA path never run |
| 3 | Tripo & Meshy | 🟡 | Full adapters tested against fakes built from the API docs; no paid key yet |
| 4 | Local AI generation engine | ✅ | TripoSR (Phase 8), verified end to end on a real photo on a CPU; mirrored/inside-out output found and fixed in Phase 11; GPU and other OSes untested |
| 5 | Hybrid local/cloud | 🟡 | Automatic keeps photos local when it can and checks the machine first; failed jobs offer the other route with consent (Phase 10). Cloud side tested only with the mock provider |
| 6 | Professional viewer | 🟡 | Measuring, lighting presets, normals/UV views, section, screenshots up to 4× and version comparison (Phase 12), tested on rendered pixels; levels of detail with automatic switching and progressive loading (2026-11-06) |
| 7 | 3D editing | 🟡 | Transforms, delete, mirror, pivot, ground/centre and plane cuts with filled caps (Phase 11), tested on real models; no merge/split or vertex editing |
| 8 | Geometry refinement & retopology | 🟡 | Clean-up, decimation, smoothing, QuadriFlow/voxel retopology with a measured fidelity check (Phase 13), verified on real models |
| 9 | Texture & material editing | 🟡 | Material values, texture replace, UV layout (Phase 11); normal and occlusion map baking (Phase 13); no texture adjustments or painting |
| 10 | AI model enhancement | 🟡 | Local AI texture upscaling (Real-ESRGAN, verified on real images) and cloud re-texturing with consent (Meshy, Tripo; fakes only) — Phase 14 |
| 11 | Model quality analysis | ✅ | Run on real AI output, a real photogrammetry scan and Draco files (Phase 9); now flags stray and shattered surfaces |
| 12 | Version management | ✅ | Immutable versions with provenance, save edits, switch current; tested end to end |
| 13 | Export | ✅ | Seven formats re-opened and compared, now including per-vertex colours; STL/3MF Z-up; verified on real AI output (Phase 9) |
| 14 | Project management | ✅ | Create/edit/search/archive/delete; importing 3D files, backup and restore (Phase 15), tested with real files and in the browser |
| 15 | Desktop app | 🟡 | Private engine, token, Save dialog, bundled release build; verified on macOS Intel only |
| 16 | Performance & resources | 🟡 | Memory and disk checks, one heavy job at a time (Phase 10); 3M-triangle models measured and sped up (Phase 12) |

## Details

### 1. Real single-image-to-3D generation — 🟡
- **Have:** the full job pipeline is tested with the mock provider:
  - consent before upload, metadata-free photo copies and live progress;
  - validation and a GLB model card;
  - cancel, retry and recovery after a restart.
- **Have:** Tripo and Meshy single-image adapters (see 3).
- **Missing:**
  - **No single real model has ever been generated.**
  - Without a paid key the app cannot turn one photo into 3D at all.
  - There is no background removal or object segmentation step.

### 2. Real multi-image-to-3D reconstruction — 🟡
- **Have:** COLMAP 4 photogrammetry:
  - features, matching, mapping, filtering, surface, texture and upright
    orientation;
  - real-world scale from one measurement;
  - tested with real COLMAP on synthetic rendered captures, in the desktop
    app and in a read-only container.
- **Have:** Tripo/Meshy multi-view, against fakes only.
- **Missing:**
  - Never run on real photographs.
  - The dense (CUDA) path is implemented but never run (no NVIDIA GPU).
  - No object masking: the surroundings are reconstructed and then partly
    removed by ground-plane removal.

### 3. Tripo and Meshy integrations — 🟡
- **Have:** submit, poll, download, cancel (Meshy), error mapping, rate
  limits, expired-link refresh, cost estimates and encrypted key storage.
- **Missing:** a real run, which needs a paid key. Draco-compressed output
  would probably be rejected, because trimesh cannot validate glTF Draco.

### 4. Local AI generation engine — ✅
- TripoSR (Phase 8), installed on request and run in its own environment;
  verified on a real photo on a CPU. GPU and other systems are untested.

### 5. Hybrid local/cloud processing — 🟡
- **Have:**
  - Automatic / Local / Cloud modes; a project-level cloud permission and
    per-job consent.
  - Automatic picks local photogrammetry for real capture sets.
  - A local-only project can never reach a cloud provider (tested).
- **Added in Phase 10:**
  - Routing checks memory and disk: a too-small computer is never sent to
    local AI, and Automatic uses the cloud instead (with consent).
  - A failed job offers the other route; a cloud choice still needs consent,
    and Local mode is never offered the cloud.
- **Not verified:** the cloud side against real Tripo/Meshy.

### 6. Professional 3D model viewer — 🟡
- **Have:**
  - WebGL viewer with material, solid and wireframe modes.
  - Grid, axes and bounds; camera presets.
  - Hierarchy and statistics; real-world size once scaled.
  - Tested in a real browser.
- **Added in Phase 12:** point-to-point measuring, lighting presets,
  normals and UV-checker views, a section plane, screenshots (browser and
  desktop) and side-by-side version comparison. A 3.1-million-triangle model
  opens in seconds and stays responsive (measured in a browser test). Draco
  is tested end to end since Phase 9.
- **Completed 2026-11-05:** screenshots at 1×, 2× or 4× the view; parts
  without normals shaded flat; problems shown on the model. Every tool is
  checked on rendered pixels in `e2e/viewer-rendering.spec.ts`.
- **Added 2026-11-06:** levels of detail for models over 500,000 triangles,
  switched by on-screen size (or chosen by hand), and the coarsest level shown
  while the full file loads (D58).
- **Missing:**
  - Cross-fading between levels.

### 7. Actual 3D editing — 🟡
- **Have:** move, rotate and scale per part (gizmo and numbers); material
  colour, roughness, metalness and opacity; undo/redo; save as a new version
  (re-validated on the server).
- **Added in Phase 11:** delete parts, mirror, pivot to centre, place on the
  ground, centre on the origin, cut by a plane with filled caps. Tested on a
  real AI model, a two-part textured model in the browser, and with the
  server's checks on the saved versions.
- **Missing:**
  - Merge or split.
  - Cutting a single part, or skinned/instanced parts.
  - Any mesh-level (vertex/face) editing. The app does not claim to offer it.

### 8. Geometry refinement and retopology — 🟡
- **Have:**
  - Clean-up, loose-fragment removal, consistent normals, hole filling
    (untextured meshes only) and decimation.
  - Ground removal and real-world scale.
  - Blender texture baking for textured models over budget.
  - All verified with real tools.
- **Added in Phase 13:** Taubin smoothing; QuadriFlow and voxel remeshing
  kept only when a measured fidelity check passes; colours carried over and
  textures rebaked; normal and ambient-occlusion maps.
- **Added 2026-11-06:** smoothing that keeps sharp edges and corners
  (bilateral normal filtering, compared with Taubin on shapes with known true
  surfaces; D59); quads from retopology kept in OBJ and FBX exports, counted
  after re-opening (D60).
- **Missing:**
  - Quads inside the GLB itself (glTF has none), and in versions edited after
    retopology; Instant Meshes.

### 9. Texture and material editing — 🟡
- **Have:** material values (above); texture size limits in Enhance;
  base-colour baking.
- **Added in Phase 11:** replace or add a base-colour texture; a UV layout
  view.
- **Missing:**
  - Roughness maps; texture painting. (Colour adjustments and tiling: added
    2026-11-05.)
  - Generating roughness maps (normal and occlusion maps: Phase 13).
  - Texture painting.

### 10. AI model enhancement — 🟡
- **Added in Phase 14:** re-texturing by Meshy (any model up to 100 MB) or
  Tripo (its own models), with consent; the result is a new version that says
  its textures are made up.
- **Also in Phase 14:** local AI texture upscaling (Real-ESRGAN compact,
  ×2/×4), verified on real images; sharper, not more accurate, as the UI
  says.
- **Missing:**
  - AI-assisted completion of photogrammetry holes.
  - Calls to the real services (no paid key).

### 11. Model quality analysis — 🟡
- **Have:**
  - Report v2: boundary and non-manifold edges, duplicates, winding,
    inside-out surfaces (Phase 11), volume, UV range, texture sizes,
    provenance and scale status.
  - Photo quality and overlap measurement.
  - All tested on generated, scanned and broken meshes; very large meshes
    (6 million triangles) and Draco files since Phase 12 and Phase 9.
  - Open and non-manifold edges drawn on the model (2026-11-05).
- **Missing:**
  - Never run on real provider output (PBR materials from Tripo/Meshy).

### 12. Model version management — ✅
- **Have:** immutable versions, provenance and notes; edited and enhanced
  versions; make current; history view. Tested end to end.
- **Gaps** (not needed for the vision): delete one version; compare two
  versions visually.

### 13. Export — ✅
- **Have:** GLB, glTF (zip), OBJ+MTL (zip), STL and 3MF (millimetres), USDZ,
  and FBX with Blender. Every file is re-opened by an independent reader and
  compared. Tested end to end, including in containers.

### 14. Project management — ✅
- **Have:** create, edit, search, filter, archive/restore and delete,
  persisted and tested end to end.
- **Added in Phase 15:** importing GLB/OBJ/STL/PLY/3MF files and ZIPs;
  project backup and restore.
- **Since 2026-11-05:** FBX import (through Blender).
- **Missing:** duplicating a project directly.

### 15. Desktop functionality — 🟡
- **Have:**
  - A private engine with a token, a watchdog and restart on crash.
  - The native Save dialog and the data folder.
  - A bundled release build: Quit, relaunch and the DMG checked by hand on
    macOS Intel.
- **Missing:**
  - Windows, Linux and Apple Silicon builds never made.
  - Unsigned builds; no auto-update.

### 16. Performance and resource management — 🟡
- **Have:** a concurrency cap (2 jobs), job timeouts, cancellation (kills
  COLMAP/Blender), lease recovery, hardware report and temporary-file
  cleanup.
- **Added in Phase 10:** memory and disk checks before heavy local work (at
  planning time and again when the job starts); one heavy job at a time now
  includes Blender baking.
- **Added in Phase 12:** large models measured (3.1 million triangles:
  opens in 3–6 s, 0.9 s to switch view and select); validation of very large
  models 2.7× faster; web uploads over 10 MB fixed.
- **Missing:**
  - JavaScript bundle about 580 KB.

## The most important missing capability

**A local single-image AI generation engine (area 4).** The product's core
promise, *one photo in, a 3D model out*, does not work today without a paid
cloud key, and even with one it has never produced a real model. A local
engine would:

- make the core feature work offline and free, keeping the photo on the
  computer, which fits the local-first direction;
- give the first real, testable AI-generated models. That in turn verifies
  areas 1, 5 and 11 with real data, which no amount of mock testing can;
- complete Automatic mode: a single photo could stay local.

Model choice (licences checked; weights are downloaded on request, never
bundled):

| Model | Code / weights licence | Hardware | Fit |
|---|---|---|---|
| **TripoSR** (VAST, Stability AI) | MIT / MIT | CPU possible, GPU fast | **Baseline: runs on every supported machine** |
| TRELLIS (Microsoft) | MIT / MIT | NVIDIA, ≥16 GB VRAM | Later high-quality GPU path; cannot be tested here |
| Stable Fast 3D, SPAR3D | Stability AI Community Licence (revenue cap) | GPU | Not open-source licensed; avoid as default |
| Hunyuan3D 2.x | Tencent Hunyuan Community Licence (excludes EU, UK, South Korea) | GPU | Territorial exclusions; avoid as default |
