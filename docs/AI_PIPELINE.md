# AI 3D Studio — Generation & Reconstruction Pipeline

Status: design document. **Implemented today:** upload validation, image
analysis, capture assessment, hardware detection (Phase 1). Everything else is
designed here and built in Phases 2–5 (see `IMPLEMENTATION_ROADMAP.md`).

Facts about third-party services were checked against their official docs on
2026-11-02/03; links are in §9. Items we could not confirm are marked
**unverified**. Provider APIs change, so re-check before relying on them.

---

## 1. Concepts

| Term | Meaning |
|---|---|
| **Provider** | Something that turns images into a 3D asset: a cloud API (Tripo, Meshy), a local model, or the local photogrammetry engine. |
| **Capabilities** | What a provider can do, discovered at runtime (inputs, outputs, options, limits). Never assumed. |
| **Quality profile** | Backend preset (Fast / High Quality / Professional) translated into provider-specific settings plus post-processing steps. |
| **Pipeline** | Ordered stages for one job kind (single-image, multi-view AI, photogrammetry, enhance, export). |
| **Job** | Persistent unit of work in `generation_jobs`; emits `job_events`. |
| **Provenance** | Whether output surfaces were *measured* (photogrammetry), *inferred* (AI), or *mixed*. Stored on every model version. |

## 2. Provider interface (Phase 2)

Providers live in `backend/ai3d/providers/` and implement one protocol.
The pipeline never imports a concrete provider.

```python
class Provider(Protocol):
    key: str                      # "tripo", "meshy", "local-photogrammetry", "mock"
    kind: ProviderKind            # cloud_api | local | mock

    def capabilities(self) -> ProviderCapabilities: ...
    def validate_config(self, config: ProviderConfig) -> list[ConfigIssue]: ...
    def estimate_cost(self, request: GenerationRequest) -> CostEstimate | None: ...

    def submit(self, request: GenerationRequest) -> ProviderTaskRef: ...
    def poll(self, ref: ProviderTaskRef) -> ProviderTaskStatus: ...   # status + progress
    def cancel(self, ref: ProviderTaskRef) -> CancelOutcome: ...       # may be "unsupported"
    def fetch_result(self, ref: ProviderTaskRef, dest: Path) -> ProviderResult: ...
    def verify_webhook(self, headers, raw_body: bytes) -> WebhookEvent | None: ...
```

```python
@dataclass
class ProviderCapabilities:
    input_modes: set[Literal["single_image", "multi_view"]]
    max_input_images: int
    accepted_mime_types: set[str]
    max_input_bytes: int
    output_formats: set[ExportFormat]
    supports_texture: bool
    supports_pbr: bool
    supports_quad_topology: bool
    supports_cancel: bool
    supports_webhooks: bool
    supports_progress: bool
    options: dict[str, OptionSpec]   # provider-native knobs, typed & documented
```

### Error normalisation

Every provider maps its errors into one of:
`auth_failed`, `insufficient_credits`, `rate_limited` (with `retry_after`),
`invalid_input`, `content_rejected` (moderation), `provider_unavailable`,
`task_failed`, `timeout`, `cancelled`. Retries are only automatic for
`rate_limited`, `provider_unavailable` and `timeout`.

### Credentials

Keys come from environment (`AI3D_TRIPO_API_KEY`, `AI3D_MESHY_API_KEY`) or
are stored encrypted (Fernet key from `AI3D_SECRET_KEY` / OS keychain on
desktop). The API returns only `api_key_hint` (last 4 characters). Keys are
redacted from logs and excluded from project exports.

### Mock provider

`MockProvider` (`kind=mock`) produces a deterministic primitive mesh. It exists
only for automated tests, is registered only when `AI3D_ENVIRONMENT=test`, and
its output is labelled `provider_key="mock"` everywhere.

## 3. Quality profiles → provider settings

Profiles are data, not code paths. Each provider adapter declares how a
profile maps onto its own options; unsupported aspects fall back to local
post-processing or are reported as "not available with this provider".

| Profile | Tripo v3 (see §9) | Meshy (see §9) | Local post-processing |
|---|---|---|---|
| Fast | `texture_quality=standard`, `geometry_quality=standard`, default `face_limit` | `ai_model=meshy-6-lite` or `latest` w/o remesh, `should_texture=true` | validate, drop NaN/degenerate faces |
| High Quality | `texture_quality=detailed`, `pbr=true`, `geometry_quality=detailed` | `ai_model=latest`, `enable_pbr=true`, `texture_resolution=4k` | + merge vertices, small-component removal, normal repair |
| Professional | High Quality + `quad=true` when topology requested, `export_uv=true` | + `should_remesh=true`, `topology=quad`, `target_polycount` | + decimation/retopology (Blender), UV check, texture bake, full report |

Exact parameter names are re-validated against the provider's capability
response at runtime; the table above is the starting configuration.

## 4. Single-image pipeline

```
upload ─► validate ─► preprocess ─► segment ─► analyse ─► [route] ─► generate ─► fetch
                                                                                  │
GLB export ◄─ optimise ◄─ validate mesh ◄─ texture processing ◄─ normalise mesh ◄─┘
```

| # | Stage | Implementation | Phase |
|---|---|---|---|
| 1 | Upload | Streamed to temp file, SHA-256, size limit, dedupe | **done** |
| 2 | Validation | Pillow content sniffing (JPEG/PNG/WebP), decompression-bomb guard, truncation check, animated-image rejection | **done** |
| 3 | Preprocess | EXIF orientation applied, resized to provider limits, **EXIF/GPS stripped**, sRGB | 2 |
| 4 | Segmentation | Skip if PNG/WebP already has alpha (subject coverage and frame edges checked at upload); else the local engine's U²-Net or provider-side background removal. The capture check's **Check cut-out** previews the local mask and the AI's view, and flags cropped, small or background-matching objects (`ai3d.processing.cutout`) | done for local generation |
| 5 | Quality analysis | Sharpness (variance of Laplacian), exposure, clipping, contrast, resolution | **done** |
| 6 | Generation | Provider `submit` → store `provider_task_id` immediately → `poll` | 2 |
| 7 | Retrieval | Download GLB to storage as `provider_raw` asset (checksummed) | 2 |
| 8 | Texture processing | Validate embedded textures, convert/resize to profile limits | 5 |
| 9 | Mesh validation | trimesh: finite values, degenerate faces, components, watertight, winding | 2 (basic) / 5 (full) |
| 10 | Optimisation | Merge/cleanup; decimation for Professional | 5 |
| 11 | GLB export | Write normalised GLB; reopen & verify | 2 |
| 12 | Preview | Viewer loads GLB; thumbnail rendered | 3 |

Provenance for single-image outputs is always `inferred`, and the UI says so.

## 5. Multi-view pipelines

Two different routes share the same uploaded photo set:

**A. AI multi-view (2–4 views).** Tripo multiview (front required, ≥2 views;
front/left/back/right slots) or Meshy multi-image (1–4 images). Provenance
`inferred`.

**B. Photogrammetry (3–300 photos; 20+ for a usable result).** Runs on this
computer (`ai3d_engine.reconstruction` + `workers/reconstruction.py`); nothing
is uploaded and no consent is needed. Provenance `measured`, scale unknown.

| # | Stage | Tooling | Status |
|---|---|---|---|
| 1–2 | Upload, validation | Phase 1 | done |
| 3 | Overlap assessment | OpenCV SIFT + ratio test + RANSAC (fundamental matrix, ≥ 30 inliers) on ≤ 800 px copies; all pairs up to 40 photos, neighbours + sample above | done, on request from the capture check; cached per photo set |
| 4 | Camera metadata | Upright copies keep camera EXIF (focal length) for COLMAP's intrinsics prior; GPS removed | done |
| 5–6 | Features & matching | COLMAP 4 `feature_extractor`, `exhaustive_matcher` (≤ 150 photos) / `sequential_matcher` | done (CPU) |
| 7–8 | Camera poses & sparse points | COLMAP `mapper`; largest model kept | done (CPU) |
| — | Upright orientation | Gravity = direction perpendicular to all camera x-axes (photos are upright even when tilted); floor at y = 0; scale unchanged | done |
| 9 | Dense reconstruction | COLMAP `patch_match_stereo` + `stereo_fusion` | wired for CUDA builds; **not tested** (no CUDA here) |
| 10 | Point-cloud filtering | Open3D statistical outlier removal (optional extra) | done; stored as the version's point cloud |
| 11 | Meshing | COLMAP `delaunay_mesher` (`--input_type sparse` without CUDA, `dense` with it) | sparse done; dense untested |
| 12 | Texturing | COLMAP `image_undistorter` + `mesh_texturer` (CPU) | done; failure keeps the untextured surface |
| 13–15 | Validation, GLB | trimesh conversion, the same file checks as provider output | done |

Never run: photogrammetry on a single photo — planning blocks it
(`too_few_photos`) and the engine refuses fewer than 3 photos.

Meaningful errors (tested): "COLMAP could not find two photos that overlap
enough to start…", "Only N of M photos could be placed in 3D…", photos with
few features named in the warnings, unused photos listed, separate groups
reported, COLMAP missing or older than 4.0 explained with the fix.

## 6. Hybrid routing (AUTOMATIC mode)

Inputs: project input mode, image count, quality profile, local capability
report (`/api/v1/devices/local`), enabled providers + their capabilities,
project `allow_cloud_upload`, job `cloud_upload_consented`.

```
if input is single image:
    local AI engine installed (TripoSR, CPU or CUDA) and mode is Local/Automatic → local
    else (Automatic/Cloud) cloud provider configured → cloud (needs consent)
    else → blocked with the reason (e.g. how to install the local engine)
if multi-view (implemented in services/jobs.py::_choose_kind):
    provider = local-photogrammetry, or processing_mode = local → photogrammetry
    automatic: ≥ 20 photos (or more than 4) and COLMAP 4 available → photogrammetry
      (dense on CUDA for High quality/Professional; otherwise sparse, never a cloud fallback)
    otherwise → AI multi-view with 2–4 labelled views (cloud, needs consent)
```

Rules that are never relaxed:

* `processing_mode=local` never contacts a cloud provider.
* A job that would upload images without `allow_cloud_upload`/consent stops in
  state `waiting_for_consent` (UI asks), it does **not** silently fall back.
  (`waiting_for_consent` is a `JobStatus` value; enum values are stored as
  plain strings, so adding it needed no migration.)
* Copies sent to providers have EXIF (incl. GPS) stripped; originals stay local.

## 7. Platform realities (affects routing and docs)

| Fact | Consequence |
|---|---|
| COLMAP dense stereo needs CUDA (FAQ) | No dense MVS on any Mac; sparse only. |
| pycolmap 4.2.1 wheels: win_amd64, manylinux x86_64, macOS **arm64 only**; CUDA wheels Linux-only (`pycolmap-cuda12`) | Intel Macs must use the COLMAP CLI (Homebrew `colmap` 4.2.1) instead of pycolmap. |
| Open3D 0.20.0 (2026-09-16): macOS wheels arm64-only; Intel macOS dropped | Intel Macs use Open3D 0.19.0 (universal2 wheel; verified with NumPy 2.4 on macOS 26 Intel). |
| conda-forge ships COLMAP 4.2.1 CPU builds for osx-64 | Intel Macs install COLMAP without compiling (see LOCAL_GPU_SETUP.md). |
| PyTorch 2.2.2 is the last release with x86_64 macOS wheels (CPU only, needs NumPy 1.x) | The local AI engine runs in its own environment with those versions; TripoSR on the CPU takes about 1.5 minutes per photo on an Intel Mac. |
| TRELLIS / TRELLIS.2 require NVIDIA GPUs with 16 GB / 24 GB | Offer only on matching hardware. |

## 8. Local AI models (not bundled)

Model weights are **never** redistributed. Users install them and accept the
licence themselves. **TripoSR is integrated** (Phase 8: `ai3d-local-ai install`,
provider `local-triposr`, see LOCAL_GPU_SETUP.md); the others are candidates:

| Model | Code / weights licence | Min VRAM | Devices | Notes |
|---|---|---|---|---|
| TripoSR | MIT / MIT | CPU works | CPU (verified, Intel Mac); CUDA (untested) | **Integrated** (Phase 8); output refined by our own code (`processing/refine.py`, LOCAL_GPU_SETUP.md) |
| Stable Fast 3D | Stability AI Community License | ≈6 GB | CUDA; MPS experimental; CPU | Free below US$1M annual revenue; gated download (Hugging Face account and licence acceptance). Tried 2026-11-09 on an Intel Mac CPU: runs after small patches for PyTorch 2.2 (about 4 min per photo); sharper front colours and lighter meshes than raw TripoSR, shapes still guessed. Not integrated: the licence terms conflict with D8 and with shipping a product |
| SPAR3D | Stability AI Community License | 10.5 GB (≈7 GB low-VRAM) | CUDA; MPS experimental | Same licence terms |
| TRELLIS | MIT / MIT (some submodules separate) | ≥16 GB | CUDA, Linux-tested | **Integrated through your own GPU server** (`gpu-server/`, docs/GPU_SERVER.md): mesh decoding only (FlexiCubes, Apache-2.0); nvdiffrast and the Gaussian rasteriser (non-commercial) are not used; texturing is the app's own. Not yet run on a GPU |
| TRELLIS.2 (4B) | MIT / MIT | ≥24 GB | CUDA 12.4, Linux-tested | Uses nvdiffrast/nvdiffrec |
| Hunyuan3D-2 / 2.1 | Tencent Hunyuan Community License — **excludes EU, UK, South Korea**; >1M MAU needs separate licence | 6–29 GB | CUDA; MPS shape-only claims unverified | Territorial restriction must be shown before download |
| InstantMesh | Apache-2.0 / Apache-2.0 | unverified | CUDA | Requires nvdiffrast (**non-commercial NVIDIA licence**) |

## 9. Cloud providers (facts and sources)

### Tripo AI (API v3, launched 2026-04)
* Base `https://openapi.tripo3d.ai/v3`, `Authorization: Bearer <key>`.
* `POST /v3/generation/image-to-model`, `POST /v3/generation/multiview-to-model`
  (front required, ≥2 views). Images via `POST /v3/files` (JPEG/PNG ≤ 20 MB →
  `file_token`), presigned upload, or URL.
* `GET /v3/tasks/{id}`: `queued|running|success|failed|cancelled`, progress 0–100.
* Cancel endpoint: **unverified** (status exists; no documented endpoint).
* Webhooks: console-configured; `Tripo-Webhook-Signature: t=…,v1=…` HMAC-SHA256
  over `{timestamp}.{raw_body}`.
* Outputs GLB (FBX with `quad=true`); `/v3/models/convert` → GLTF/FBX/USDZ/OBJ/STL/3MF.
* Pricing: 1 credit = US$0.01; image/multiview→3D 20 credits untextured,
  30 with standard texture; add-ons (HD texture +10, 8K +20, HD geometry +20,
  quad +5…). Failed/cancelled tasks not charged.
* Output rights: free users — Tripo retains rights; paid users — generally own
  outputs (Terms §5.2). Whether API-credit buyers count as paid: **unverified**.

### Meshy AI
* Base `https://api.meshy.ai/openapi/v1`, `Authorization: Bearer msy_…`;
  **API keys require a paid plan**.
* `POST /image-to-3d` (URL or data URI, jpg/png/webp ≤ 20 MB),
  `POST /multi-image-to-3d` (1–4 images); `GET /:id`, `GET /:id/stream` (SSE).
* Status `PENDING|IN_PROGRESS|SUCCEEDED|FAILED|CANCELED`, progress 0–100.
* No cancel endpoint; `DELETE` on a `PENDING` task refunds it (409 when in progress).
* Webhooks: dashboard-only; signature verification **not documented** →
  the adapter will poll/SSE and treat webhooks only as a hint.
* Outputs glb/fbx/obj/usdz/stl/3mf; **assets retained ≤ 3 days** (non-Enterprise)
  → download immediately.
* Credits: meshy-7.1/6 — 20 (mesh), 30 (2K/4K texture), 35 (8K); meshy-6-lite
  5/15. USD per credit: **unverified**.
* Licensing: free-plan outputs CC BY 4.0; paid plans customer-owned.

Sources: developers.tripo3d.ai/en/docs (introduction, generation-image-to-model,
generation-multiview-to-model, files, task-query, task-lifecycle, webhooks,
models-convert, rate-limits, billing), developers.tripo3d.ai/en/pricing,
developers.tripo3d.ai/en/terms; docs.meshy.ai/en/api (authentication,
image-to-3d, multi-image-to-3d, webhooks, asset-retention, rate-limits,
pricing), meshy.ai/pricing; github.com/VAST-AI-Research/TripoSR;
github.com/Stability-AI/stable-fast-3d; github.com/Stability-AI/stable-point-aware-3d;
github.com/microsoft/TRELLIS; github.com/microsoft/TRELLIS.2;
github.com/Tencent-Hunyuan/Hunyuan3D-2 and -2.1; github.com/TencentARC/InstantMesh;
github.com/NVlabs/nvdiffrast (LICENSE); colmap.github.io (license, faq, install);
pypi.org/project/pycolmap, pycolmap-cuda12, open3d;
github.com/isl-org/Open3D/releases/tag/v0.20.0.

## 10. Job lifecycle

```
queued ──claim──► running ──► succeeded
   │                 │  └──► failed (error_code, error_message, details)
   │                 └──► cancelling ──► cancelled
   └──► cancelled
```

Every transition writes a `job_events` row. Restart recovery: expired leases
with a stored `provider_task_id` resume polling; others fail with
`interrupted` and can be retried (`POST /jobs/{id}/retry` creates a new job
with `parent_job_id`). Idempotency: `Idempotency-Key` header → unique
`generation_jobs.idempotency_key`; a repeated request returns the original job.
