# AI provider configuration

> **Status (Phase 2):** the Tripo and Meshy adapters are implemented and tested
> against recorded-format fake servers. **Neither has been run against the real
> service yet** (no API key was available during development), so real
> generation is untested until someone runs it with a key. This page documents
> the configuration and — more importantly — what using each provider costs and
> implies, so you can decide before enabling one.
> Facts were checked on 2026-11-02/03 against the providers' official
> documentation (links in `docs/AI_PIPELINE.md` §9). Prices change; always
> confirm on the provider's site.

## Principles

- **Optional.** AI 3D Studio works without any provider (upload, analysis,
  capture check now; local photogrammetry in Phase 4).
- **Your key, server-side only.** Keys are read by the backend from the
  environment (or stored encrypted). They never reach the browser, logs or
  exported projects.
- **Your images leave your machine only with permission.** A project must
  allow cloud upload, and a job must be consented to. Copies sent to a
  provider have EXIF/GPS metadata removed.
- **Costs are the provider's.** AI 3D Studio has no billing. It shows the
  provider-reported cost of each job when the provider reports one.

## Configuration

Either set the key in the backend environment:

```dotenv
# .env (backend)
AI3D_TRIPO_API_KEY=tsk_…
AI3D_MESHY_API_KEY=msy_…
```

and restart the backend, or paste it in **Settings → AI providers**. Keys
entered there are stored encrypted with `AI3D_SECRET_KEY`; if that is unset, a
random key is created in `<data dir>/secret.key` (keep that file private). An
environment key takes precedence over a stored one. The API and UI only ever
show the last four characters. **Check** calls the provider's balance endpoint
to confirm the key works (this costs nothing). OS keychain storage for the
desktop app is planned for Phase 6.

Then, for each generation: the project must allow cloud upload, and you tick
the per-job consent box. A job created without consent waits in
**Waiting for permission** and uploads nothing until you approve or decline it.

The `mock` provider seen in tests exists only when `AI3D_ENVIRONMENT=test`; it
returns a fixed placeholder sphere and is never offered in normal use.

## Tripo AI

| | |
|---|---|
| API | v3 (`https://openapi.tripo3d.ai/v3`), Bearer key |
| Inputs | single image; multi-view (front required, 2–4 views: front/left/back/right) — JPEG/PNG ≤ 20 MB |
| Output | GLB (FBX with quad topology); conversion to GLTF/FBX/USDZ/OBJ/STL/3MF via API |
| Progress / cancel | progress 0–100; cancel endpoint **unverified** |
| Webhooks | signed (HMAC-SHA256), configured in the Tripo console |
| Cost | 1 credit = US$0.01. Image→3D or multiview→3D: **20 credits** untextured, **30** with standard texture. Add-ons: HD texture +10, 8K texture +20, HD geometry +20, quad +5, smart low-poly +10, parts +20. Failed/cancelled tasks are not charged. |
| Limits | concurrency per account (e.g. 10 for current 3D models); 429 on excess |
| Output rights | Terms (2025-07-11): free users — Tripo retains rights; paid users — generally own outputs; whether API-credit purchases count as "paid": **unverified**. Outputs may not be used to build competing services. |

## Meshy AI

| | |
|---|---|
| API | `https://api.meshy.ai/openapi/v1`, Bearer key — **API access requires a paid Meshy plan** |
| Inputs | image-to-3D (URL or data URI, jpg/png/webp ≤ 20 MB); multi-image (1–4 images) |
| Output | GLB, FBX, OBJ+MTL, USDZ, STL, 3MF (on request) |
| Progress / cancel | progress 0–100, SSE stream; no cancel — pending tasks can be deleted (refunded) |
| Webhooks | dashboard-configured; **no documented signature** → AI 3D Studio polls instead of trusting webhooks |
| Retention | API assets kept **≤ 3 days** (non-Enterprise) — AI 3D Studio downloads results immediately |
| Cost | credits per task: meshy-7.1 / meshy-6 — 20 (mesh only), 30 (2K/4K texture), 35 (8K); meshy-6-lite — 5 / 15. USD per credit depends on plan (**unverified**). Failed tasks refunded. |
| Limits | 20 req/s; concurrent tasks 10–100+ by plan |
| Output rights | free plan outputs CC BY 4.0 (attribution required); paid plans: customer-owned |

## Local engines

| Engine | Cost | Requirements |
|---|---|---|
| Photogrammetry (COLMAP, Phase 4) | free (BSD) | 20+ overlapping photos; CPU for poses, NVIDIA CUDA for dense surfaces |
| Local AI models (Phase 6+) | free to run; **licences vary** | typically ≥ 6 GB NVIDIA VRAM — see `AI_PIPELINE.md` §8 for licences (Stability Community License revenue cap, Hunyuan territory exclusions, nvdiffrast non-commercial) |

## Choosing a provider for a quality mode

Quality modes are translated into each provider's real options (see
`AI_PIPELINE.md` §3). If a provider lacks an option (e.g. PBR), the job either
uses local post-processing or tells you the option is unavailable — it never
pretends.
