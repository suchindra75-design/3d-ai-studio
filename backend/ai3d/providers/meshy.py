"""Meshy AI adapter.

Wire format from https://docs.meshy.ai/en/api (checked 2026-11-03):

* ``POST /image-to-3d`` ``{"image_url": <URL or data URI>, …}`` and
  ``POST /multi-image-to-3d`` ``{"image_urls": [...1–4]}`` → 202 ``{"result": id}``
* ``GET /{image-to-3d|multi-image-to-3d}/{id}`` → ``status``
  (PENDING|IN_PROGRESS|SUCCEEDED|FAILED|CANCELED), ``progress`` 0–100,
  ``model_urls.glb``, ``thumbnail_url``, ``consumed_credits``, ``task_error``
* ``DELETE`` on a PENDING task deletes and refunds it; IN_PROGRESS → 409
* ``POST /retexture`` ``{"model_url": <URL or data URI ≤ 100 MB>, "text_style_prompt",
  "enable_pbr", "texture_resolution", …}`` → ``{"result": id}``; ``GET /retexture/{id}``
  as above (checked 2026-11-04; the price per task is not documented)
* ``GET /balance`` → ``balance``
* Errors are ``{"message": …}`` with 400/401/402/403/404/422/429/5xx.

API access requires a paid Meshy plan; assets are kept for 3 days and their
links are signed, so results are downloaded as soon as a task succeeds.
"""

import base64
from pathlib import Path
from typing import Any

import httpx2

from ai3d.models.enums import JobKind, ProviderKind, QualityMode
from ai3d.providers.base import (
    DEFAULT_MAX_MODEL_BYTES,
    VIEW_ORDER,
    CancelOutcome,
    CostEstimate,
    CredentialCheck,
    GenerationRequest,
    MultiViewLimits,
    Provider,
    ProviderCapabilities,
    ProviderError,
    ProviderErrorCode,
    ProviderInput,
    ProviderPlan,
    ProviderResult,
    TaskState,
    TaskStatus,
)
from ai3d.providers.http import api_client, download, network_error, parse_retry_after

NAME = "Meshy AI"
MAX_INPUT_BYTES = 20 * 1024 * 1024
PREVIEW_MAX_BYTES = 10 * 1024 * 1024
_SINGLE = "image-to-3d"
_MULTI = "multi-image-to-3d"
_RETEXTURE = "retexture"
# Retexture accepts a model as a data URI up to this size (docs, checked 2026-11-04).
RETEXTURE_MAX_BYTES = 100 * 1024 * 1024

_PROFILES: dict[QualityMode, dict[str, Any]] = {
    QualityMode.FAST: {
        "ai_model": "meshy-6-lite",
        "should_texture": True,
        "texture_resolution": "2k",
    },
    QualityMode.HIGH_QUALITY: {
        "ai_model": "latest",
        "should_texture": True,
        "enable_pbr": True,
        "texture_resolution": "4k",
    },
    QualityMode.PROFESSIONAL: {
        "ai_model": "latest",
        "should_texture": True,
        "enable_pbr": True,
        "texture_resolution": "4k",
        "geometry_resolution": "2k",
    },
}

# Published credit prices (docs.meshy.ai/en/api/pricing, checked 2026-11-03).
_CREDITS: dict[QualityMode, int] = {
    QualityMode.FAST: 15,  # meshy-6-lite with 2K textures
    QualityMode.HIGH_QUALITY: 30,  # latest with 2K/4K textures
    QualityMode.PROFESSIONAL: 35,  # + ultra geometry (+5)
}

_STATE: dict[str, TaskState] = {
    "PENDING": "queued",
    "IN_PROGRESS": "running",
    "SUCCEEDED": "succeeded",
    "FAILED": "failed",
    "CANCELED": "cancelled",
}


def _data_uri(item: ProviderInput) -> str:
    encoded = base64.b64encode(item.path.read_bytes()).decode("ascii")
    return f"data:{item.mime_type};base64,{encoded}"


class MeshyProvider(Provider):
    key = "meshy"
    display_name = NAME
    kind = ProviderKind.CLOUD_API
    uploads_images = True
    description = (
        "Cloud image-to-3D API. Your photos are sent to Meshy and generation is billed "
        "to your Meshy account in credits."
    )
    homepage_url = "https://www.meshy.ai"
    docs_url = "https://docs.meshy.ai/en/api"
    pricing_url = "https://docs.meshy.ai/en/api/pricing"
    api_key_setting = "meshy_api_key"

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None = None,
        transport: httpx2.BaseTransport | None = None,
        max_model_bytes: int = DEFAULT_MAX_MODEL_BYTES,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            transport=transport,
            max_model_bytes=max_model_bytes,
        )
        if not api_key:
            raise ProviderError(ProviderErrorCode.AUTH_FAILED, "No Meshy API key is configured.")
        self._client = api_client(base_url or "https://api.meshy.ai/openapi/v1", api_key, transport)

    @classmethod
    def capabilities(cls) -> ProviderCapabilities:
        return ProviderCapabilities(
            single_image=True,
            multi_view=MultiViewLimits(min_images=2, max_images=4, required_roles=("front",)),
            accepted_mime_types=("image/jpeg", "image/png", "image/webp"),
            max_input_bytes=MAX_INPUT_BYTES,
            output_formats=("glb",),
            supports_texture=True,
            supports_pbr=True,
            cancel="pending_only",
            reports_progress=True,
            retexture=True,
            notes=(
                "API access requires a paid Meshy plan.",
                "Cancelling refunds a task only before Meshy starts it; started tasks are charged.",
                "Meshy keeps results for 3 days; they are downloaded immediately.",
            ),
        )

    @classmethod
    def plan(cls, kind: JobKind, quality_mode: QualityMode) -> ProviderPlan:
        if kind == JobKind.RETEXTURE:
            pbr = quality_mode != QualityMode.FAST
            return ProviderPlan(
                settings={
                    "enable_pbr": pbr,
                    "texture_resolution": "4k"
                    if quality_mode == QualityMode.PROFESSIONAL
                    else "2k",
                    "target_formats": ["glb"],
                },
                summary=(
                    "Meshy Retexture",
                    "PBR materials" if pbr else "Base colour only",
                    "4K textures" if quality_mode == QualityMode.PROFESSIONAL else "2K textures",
                ),
                estimate=None,
                notes=(
                    "Meshy charges credits for retexturing; its API documentation does not "
                    "state the price (see your Meshy plan). Failed tasks are refunded.",
                ),
            )
        profile = _PROFILES[quality_mode]
        summary = [
            "Model meshy-6-lite" if profile["ai_model"] == "meshy-6-lite" else "Latest Meshy model",
            f"{profile['texture_resolution'].upper()} textures",
            "PBR materials" if profile.get("enable_pbr") else "Base colour only",
        ]
        if profile.get("geometry_resolution"):
            summary.append("Ultra (2K) geometry")
        return ProviderPlan(
            settings={**profile, "target_formats": ["glb"]},
            summary=tuple(summary),
            estimate=CostEstimate(
                amount=_CREDITS[quality_mode],
                unit="credits",
                usd=None,
                basis="Meshy's published credit prices (checked 2026-11-03). The US$ value of "
                "a credit depends on your Meshy plan. Failed tasks are refunded.",
            ),
        )

    # -- HTTP ---------------------------------------------------------------

    def _error(self, response: httpx2.Response) -> ProviderError:  # noqa: PLR0911 - one exit per documented error
        try:
            body = response.json()
            message = str(body.get("message") or "") if isinstance(body, dict) else ""
        except ValueError:
            message = ""
        status = response.status_code
        details: dict[str, Any] = {"http_status": status}
        text = message or f"HTTP {status}"
        if status == 401:
            return ProviderError(
                ProviderErrorCode.AUTH_FAILED, "Meshy rejected the API key.", details=details
            )
        if status == 402:
            return ProviderError(
                ProviderErrorCode.INSUFFICIENT_CREDITS,
                "Your Meshy account has no credits left or no API access (paid plan required).",
                details=details,
            )
        if status == 403:
            if "ip_not_authorized" in message:
                return ProviderError(
                    ProviderErrorCode.AUTH_FAILED,
                    "Meshy refused this IP address (check the key's IP allowlist).",
                    details=details,
                )
            return ProviderError(
                ProviderErrorCode.CONTENT_REJECTED,
                f"Meshy's moderation rejected the image. {message}".strip(),
                details=details,
            )
        if status == 429:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            return ProviderError(
                ProviderErrorCode.RATE_LIMITED,
                "Meshy's rate or concurrency limit was reached.",
                retry_after=30.0 if retry_after is None else retry_after,
                details=details,
            )
        if status == 404:
            return ProviderError(
                ProviderErrorCode.TASK_FAILED, "Meshy could not find the task.", details=details
            )
        if 400 <= status < 500:
            if "moderat" in message.lower():
                return ProviderError(
                    ProviderErrorCode.CONTENT_REJECTED,
                    f"Meshy's moderation rejected the image. {message}",
                    details=details,
                )
            return ProviderError(
                ProviderErrorCode.INVALID_INPUT,
                f"Meshy rejected the request: {text}",
                details=details,
            )
        return ProviderError(
            ProviderErrorCode.PROVIDER_UNAVAILABLE,
            f"Meshy is unavailable (HTTP {status}).",
            details=details,
        )

    def _json(self, response: httpx2.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise self._error(response)
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE, "Meshy returned an unreadable response."
            ) from exc
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _split(task_ref: str) -> tuple[str, str]:
        endpoint, _, task_id = task_ref.partition(":")
        if endpoint not in {_SINGLE, _MULTI, _RETEXTURE} or not task_id:
            raise ProviderError(
                ProviderErrorCode.TASK_FAILED, f"Unrecognised Meshy task reference {task_ref!r}."
            )
        return endpoint, task_id

    # -- Provider -----------------------------------------------------------

    def submit(self, request: GenerationRequest) -> str:
        if request.kind == JobKind.RETEXTURE:
            endpoint = _RETEXTURE
            settings = dict(request.settings)
            prompt = str(settings.pop("prompt", "") or "").strip()
            settings.pop("source_task_id", None)
            if not prompt:
                raise ProviderError(
                    ProviderErrorCode.INVALID_INPUT, "Describe the texture you want for Meshy."
                )
            model = request.inputs[0]
            if model.path.stat().st_size > RETEXTURE_MAX_BYTES:
                raise ProviderError(
                    ProviderErrorCode.INVALID_INPUT,
                    "Meshy accepts models up to 100 MB sent directly; this one is larger.",
                )
            encoded = base64.b64encode(model.path.read_bytes()).decode("ascii")
            body: dict[str, Any] = {
                "model_url": f"data:application/octet-stream;base64,{encoded}",
                "text_style_prompt": prompt[:800],
                **settings,
            }
        elif request.kind == JobKind.IMAGE_TO_3D:
            endpoint = _SINGLE
            body = {"image_url": _data_uri(request.inputs[0]), **request.settings}
        else:
            endpoint = _MULTI
            # The first image is treated as the front view.
            ordered = sorted(request.inputs, key=lambda item: VIEW_ORDER.index(item.role))
            body = {"image_urls": [_data_uri(item) for item in ordered], **request.settings}
        try:
            response = self._client.post(f"/{endpoint}", json=body)
        except httpx2.ConnectError as exc:
            raise network_error(NAME, exc) from exc
        except httpx2.HTTPError as exc:
            raise ProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "The connection to Meshy was lost while creating the task. It may have been "
                "created; check your Meshy dashboard before retrying.",
                retryable=False,
                details={"submission_uncertain": True},
            ) from exc
        task_id = self._json(response).get("result")
        if not isinstance(task_id, str) or not task_id:
            raise ProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE, "Meshy did not return a task id."
            )
        return f"{endpoint}:{task_id}"

    def poll(self, task_id: str) -> TaskStatus:
        endpoint, raw_id = self._split(task_id)
        try:
            data = self._json(self._client.get(f"/{endpoint}/{raw_id}"))
        except httpx2.HTTPError as exc:
            raise network_error(NAME, exc) from exc
        raw_status = str(data.get("status") or "")
        state = _STATE.get(raw_status, "running")
        progress = data.get("progress")
        status = TaskStatus(
            state=state,
            progress=max(0.0, min(1.0, float(progress) / 100))
            if isinstance(progress, int | float)
            else None,
            raw={"status": raw_status, "progress": progress},
        )
        credits = data.get("consumed_credits")
        if isinstance(credits, int | float) and state in {"succeeded", "failed"}:
            status.cost = {"credits": float(credits), "usd": None, "source": "provider"}
        model_urls = data.get("model_urls")
        if isinstance(model_urls, dict) and isinstance(model_urls.get("glb"), str):
            status.outputs["model"] = model_urls["glb"]
        if isinstance(data.get("thumbnail_url"), str) and data["thumbnail_url"]:
            status.outputs["preview"] = data["thumbnail_url"]
        if state == "failed":
            raw_error = data.get("task_error")
            error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
            message = str(error.get("message") or "Meshy reported that generation failed.")
            if error.get("code") == "moderation_blocked":
                code = ProviderErrorCode.CONTENT_REJECTED
            elif error.get("type") == "invalid_input":
                code = ProviderErrorCode.INVALID_INPUT
            elif error.get("type") == "timeout":
                code = ProviderErrorCode.TIMEOUT
            else:
                code = ProviderErrorCode.TASK_FAILED
            status.raw["task_error"] = {k: error.get(k) for k in ("type", "code")}
            status.error = ProviderError(code, message, retryable=False)
            status.message = message
        return status

    def cancel(self, task_id: str) -> CancelOutcome:
        endpoint, raw_id = self._split(task_id)
        current = self.poll(task_id)
        if current.state == "cancelled":
            return "cancelled"
        if current.state != "queued":
            return "too_late"
        try:
            response = self._client.delete(f"/{endpoint}/{raw_id}")
        except httpx2.HTTPError as exc:
            raise network_error(NAME, exc) from exc
        if response.status_code == 409:
            return "too_late"
        if response.status_code >= 400:
            raise self._error(response)
        return "cancelled"

    def download_result(self, task_id: str, status: TaskStatus, dest_dir: Path) -> ProviderResult:
        url = status.outputs.get("model")
        if not url:
            raise ProviderError(
                ProviderErrorCode.INVALID_OUTPUT, "Meshy did not return a GLB file."
            )
        mesh_path = dest_dir / "model.glb"
        download(
            url,
            mesh_path,
            max_bytes=self._max_model_bytes,
            transport=self._transport,
            provider=NAME,
        )
        preview_path: Path | None = None
        if preview := status.outputs.get("preview"):
            candidate = dest_dir / "preview"
            try:
                download(
                    preview,
                    candidate,
                    max_bytes=PREVIEW_MAX_BYTES,
                    transport=self._transport,
                    provider=NAME,
                )
                preview_path = candidate
            except ProviderError:
                preview_path = None
        return ProviderResult(mesh_path=mesh_path, mesh_format="glb", preview_path=preview_path)

    def check_credentials(self) -> CredentialCheck:
        try:
            data = self._json(self._client.get("/balance"))
        except httpx2.HTTPError as exc:
            raise network_error(NAME, exc) from exc
        balance = data.get("balance")
        if isinstance(balance, int | float):
            return CredentialCheck(
                ok=True,
                message=f"Key accepted. Balance: {balance:g} credits.",
                balance=float(balance),
                unit="credits",
            )
        return CredentialCheck(ok=True, message="Key accepted.")

    def close(self) -> None:
        self._client.close()
