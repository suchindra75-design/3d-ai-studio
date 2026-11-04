# Capability audit (2026-11-04, updated after Phase 8)

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
| 2 | Multi-image → 3D | 🟡 | COLMAP photogrammetry works end to end, but only on synthetic renders; dense CUDA path never run |
| 3 | Tripo & Meshy | 🟡 | Full adapters tested against fakes built from the API docs; no paid key yet |
| 4 | Local AI generation engine | ✅ | TripoSR (Phase 8), verified end to end on a real photo on a CPU; GPU and other OSes untested |
| 5 | Hybrid local/cloud | 🟠 | Automatic now keeps single photos local when the engine is installed; no offered fallback, no resource checks |
| 6 | Professional viewer | 🟠 | Solid basics; no measuring, lighting, normals/UV views, clipping or large-model handling |
| 7 | 3D editing | 🟠 | Object transforms and material values with undo; no part or mesh-level edits |
| 8 | Geometry refinement & retopology | 🟠 | Clean-up, decimation and baking work; retopology, smoothing and remeshing missing |
| 9 | Texture & material editing | 🟠 | Colour/roughness/metalness/opacity only; no texture replace, UV view or map baking |
| 10 | AI model enhancement | ❌ | "Enhance" is geometric processing; no AI refinement or texture enhancement |
| 11 | Model quality analysis | 🟡 | Thorough reports, tested on mock spheres and synthetic scans, never on real AI output |
| 12 | Version management | ✅ | Immutable versions with provenance, save edits, switch current; tested end to end |
| 13 | Export | ✅ | Seven formats, each re-opened and compared; tested end to end (FBX with Blender) |
| 14 | Project management | ✅ | Create/edit/search/archive/delete, tested end to end; importing existing models is missing |
| 15 | Desktop app | 🟡 | Private engine, token, Save dialog, bundled release build; verified on macOS Intel only |
| 16 | Performance & resources | 🟠 | Job concurrency, timeouts, cancel and recovery; no RAM/disk checks, no large-model handling |

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

### 4. Local AI generation engine — ❌
- `JobKind.IMAGE_TO_3D` has no local provider. Hardware detection reports
  whether a GPU could run one; that is all.

### 5. Hybrid local/cloud processing — 🟠
- **Have:**
  - Automatic / Local / Cloud modes; a project-level cloud permission and
    per-job consent.
  - Automatic picks local photogrammetry for real capture sets.
  - A local-only project can never reach a cloud provider (tested).
- **Missing:**
  - With no local AI, Automatic always means cloud for one to four photos.
  - No offered fallback when one route fails; the user retries by hand.
  - Routing ignores the machine's capacity, beyond reporting it.

### 6. Professional 3D model viewer — 🟠
- **Have:**
  - WebGL viewer with material, solid and wireframe modes.
  - Grid, axes and bounds; camera presets.
  - Hierarchy and statistics; real-world size once scaled.
  - Tested in a real browser.
- **Missing:**
  - Point-to-point measuring.
  - Lighting and environment presets.
  - Normals, UV and texture inspection views.
  - Section/clipping, screenshots and side-by-side version comparison.
  - Level of detail or progressive loading for large models.
  - Draco untested end to end.

### 7. Actual 3D editing — 🟠
- **Have:** move, rotate and scale per part (gizmo and numbers); material
  colour, roughness, metalness and opacity; undo/redo; save as a new version
  (re-validated on the server).
- **Missing:**
  - Hide or delete parts.
  - Set origin/pivot; place on the ground; mirror.
  - Crop or cut by a plane.
  - Merge or split.
  - Any mesh-level (vertex/face) editing. The app does not claim to offer it.

### 8. Geometry refinement and retopology — 🟠
- **Have:**
  - Clean-up, loose-fragment removal, consistent normals, hole filling
    (untextured meshes only) and decimation.
  - Ground removal and real-world scale.
  - Blender texture baking for textured models over budget.
  - All verified with real tools.
- **Missing:**
  - Retopology: QuadriFlow moved open surfaces in tests, and GLB stores
    triangles only, so it is not offered.
  - Smoothing and remeshing.
  - Normal-map baking: baking carries base colour only.

### 9. Texture and material editing — 🟠
- **Have:** material values (above); texture size limits in Enhance;
  base-colour baking.
- **Missing:**
  - Replacing or adjusting a texture.
  - A UV layout view.
  - Generating normal, roughness or ambient-occlusion maps.
  - Texture painting.

### 10. AI model enhancement — ❌
- No AI step after generation. The candidates:
  - provider refine/retexture APIs (cloud, needs consent);
  - local texture super-resolution;
  - AI-assisted completion of photogrammetry holes.

### 11. Model quality analysis — 🟡
- **Have:**
  - Report v2: boundary and non-manifold edges, duplicates, winding, volume,
    UV range, texture sizes, provenance and scale status.
  - Photo quality and overlap measurement.
  - All tested on generated, scanned and broken meshes.
- **Missing:**
  - Never run on real provider output: PBR materials, Draco, very large
    meshes.
  - Problems are listed but not highlighted on the model.

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
- **Missing:**
  - Importing existing 3D files: `ModelVersionSource.IMPORTED` exists but
    nothing uses it.
  - Duplicating a project; project backup/export.

### 15. Desktop functionality — 🟡
- **Have:**
  - A private engine with a token, a watchdog and restart on crash.
  - The native Save dialog and the data folder.
  - A bundled release build: Quit, relaunch and the DMG checked by hand on
    macOS Intel.
- **Missing:**
  - Windows, Linux and Apple Silicon builds never made.
  - Unsigned builds; no auto-update.

### 16. Performance and resource management — 🟠
- **Have:** a concurrency cap (2 jobs), job timeouts, cancellation (kills
  COLMAP/Blender), lease recovery, hardware report and temporary-file
  cleanup.
- **Missing:**
  - No RAM or disk checks before heavy local work.
  - Nothing stops two heavy local jobs (COLMAP plus Blender) from running at
    once.
  - No viewer handling for multi-million-triangle models.
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
