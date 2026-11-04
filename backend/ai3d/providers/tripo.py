"""Tripo AI adapter (API v3).

Wire format from https://developers.tripo3d.ai/en/docs (checked 2026-11-03):

* ``POST /files`` (multipart field ``file``, JPEG/PNG ≤ 20 MB) → ``data.file_token``
* ``POST /generation/image-to-model`` ``{"input": token, "model": …}`` and
  ``POST /generation/multiview-to-model`` ``{"inputs": [{"front": token}, …]}``
  → ``data.task_id``
* ``GET /tasks/{id}`` → ``data.status`` (queued|running|success|failed|cancelled,
  legacy banned|expired), ``progress`` 0–100, ``output.model_url``,
  ``output.rendered_image_url``, ``credits_consumed``
* ``GET /account/balance`` → ``data.balance``
* Success bodies are ``{"code": 0, "data": …}``; errors carry ``code`` and ``message``.

There is no documented cancel endpoint, and model URLs expire 5 minutes after
completion, so results are downloaded as soon as a task succeeds.
"""

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

NAME = "Tripo AI"
MODEL_VERSION = "v3.1-20260211"
USD_PER_CREDIT = 0.01
MAX_INPUT_BYTES = 20 * 1024 * 1024
PREVIEW_MAX_BYTES = 10 * 1024 * 1024

# Quality profile → Tripo settings. Parameter names and values per the v3
# image-to-model reference; texture/pbr default to true on Tripo's side, so
# they are always sent explicitly.
_PROFILES: dict[QualityMode, dict[str, Any]] = {
    QualityMode.FAST: {
        "texture": True,
        "pbr": False,
        "texture_quality": "standard",
        "geometry_quality": "standard",
    },
    QualityMode.HIGH_QUALITY: {
        "texture": True,
        "pbr": True,
        "texture_quality": "detailed",
        "geometry_quality": "standard",
    },
    QualityMode.PROFESSIONAL: {
        "texture": True,
        "pbr": True,
        "texture_quality": "detailed",
        "geometry_quality": "detailed",
    },
}

# Published credit prices (developers.tripo3d.ai/en/pricing, checked 2026-11-02).
_BASE_TEXTURED = 30
_HD_TEXTURE = 10
_HD_GEOMETRY = 20

_STATE: dict[str, TaskState] = {
    "queued": "queued",
    "running": "running",
    "success": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "banned": "failed",
    "expired": "failed",
}

_CODE_CONTENT_POLICY = 2008
_CODE_INSUFFICIENT_CREDITS = 2010
_CODE_CONCURRENCY = 2000
_CODE_TASK_NOT_FOUND = 2001
_AUTH_CODES = {2, 1002}


class TripoProvider(Provider):
    key = "tripo"
    display_name = NAME
    kind = ProviderKind.CLOUD_API
    uploads_images = True
    description = (
        "Cloud image-to-3D API. Your photos are uploaded to Tripo and generation "
        "is billed to your Tripo account in credits."
    )
    homepage_url = "https://www.tripo3d.ai"
    docs_url = "https://developers.tripo3d.ai/en/docs"
    pricing_url = "https://developers.tripo3d.ai/en/pricing"
    api_key_setting = "tripo_api_key"

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
            raise ProviderError(ProviderErrorCode.AUTH_FAILED, "No Tripo API key is configured.")
        self._client = api_client(base_url or "https://openapi.tripo3d.ai/v3", api_key, transport)

    # -- static description -------------------------------------------------

    @classmethod
    def capabilities(cls) -> ProviderCapabilities:
        return ProviderCapabilities(
            single_image=True,
            multi_view=MultiViewLimits(min_images=2, max_images=4, required_roles=("front",)),
            accepted_mime_types=("image/jpeg", "image/png"),
            max_input_bytes=MAX_INPUT_BYTES,
            output_formats=("glb",),
            supports_texture=True,
            supports_pbr=True,
            cancel="unsupported",
            reports_progress=True,
            notes=(
                "Tripo has no cancel API: a job cancelled here may still finish, and be "
                "charged, on Tripo's side.",
                "Download links expire 5 minutes after completion; results are downloaded "
                "immediately.",
            ),
        )

    @classmethod
    def plan(cls, kind: JobKind, quality_mode: QualityMode) -> ProviderPlan:
        profile = _PROFILES[quality_mode]
        credits = _BASE_TEXTURED
        if profile["texture_quality"] == "detailed":
            credits += _HD_TEXTURE
        if profile["geometry_quality"] == "detailed":
            credits += _HD_GEOMETRY
        summary = [
            f"Model {MODEL_VERSION}",
            "HD texture" if profile["texture_quality"] == "detailed" else "Standard texture",
            "PBR materials" if profile["pbr"] else "Base colour only",
            "HD geometry" if profile["geometry_quality"] == "detailed" else "Standard geometry",
        ]
        notes = []
        if profile["pbr"]:
            notes.append("Tripo's price list does not show a separate PBR price.")
        return ProviderPlan(
            settings={"model": MODEL_VERSION, **profile},
            summary=tuple(summary),
            estimate=CostEstimate(
                amount=credits,
                unit="credits",
                usd=round(credits * USD_PER_CREDIT, 2),
                basis="Tripo's published price list (checked 2026-11-02); 1 credit = US$0.01. "
                "Tripo reports the actual charge when the task finishes; failed tasks are "
                "not charged.",
                notes=tuple(notes),
            ),
        )

    # -- HTTP ---------------------------------------------------------------

    def _error(self, response: httpx2.Response) -> ProviderError:  # noqa: PLR0911 - one exit per documented error
        try:
            body = response.json()
        except ValueError:
            body = {}
        code = body.get("code") if isinstance(body, dict) else None
        message = str(body.get("message") or "") if isinstance(body, dict) else ""
        suggestion = str(body.get("suggestion") or "") if isinstance(body, dict) else ""
        details: dict[str, Any] = {"http_status": response.status_code, "provider_code": code}
        text = message or f"HTTP {response.status_code}"
        if suggestion:
            text = f"{text} {suggestion}"
        status = response.status_code
        if status == 401 or code in _AUTH_CODES:
            return ProviderError(
                ProviderErrorCode.AUTH_FAILED, "Tripo rejected the API key.", details=details
            )
        if code == _CODE_INSUFFICIENT_CREDITS:
            return ProviderError(
                ProviderErrorCode.INSUFFICIENT_CREDITS,
                "Your Tripo account does not have enough credits.",
                details=details,
            )
        if status == 403:
            return ProviderError(
                ProviderErrorCode.AUTH_FAILED,
                f"Tripo refused the request: {text}",
                details=details,
            )
        if status == 429:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            if retry_after is None:
                retry_after = 30.0 if code == _CODE_CONCURRENCY else 5.0
            return ProviderError(
                ProviderErrorCode.RATE_LIMITED,
                "Tripo's rate or concurrency limit was reached.",
                retry_after=retry_after,
                details=details,
            )
        if code == _CODE_CONTENT_POLICY:
            return ProviderError(
                ProviderErrorCode.CONTENT_REJECTED,
                f"Tripo's content policy rejected the image. {text}",
                details=details,
            )
        if status == 404 or code == _CODE_TASK_NOT_FOUND:
            return ProviderError(
                ProviderErrorCode.TASK_FAILED, "Tripo could not find the task.", details=details
            )
        if 400 <= status < 500:
            return ProviderError(
                ProviderErrorCode.INVALID_INPUT,
                f"Tripo rejected the request: {text}",
                details=details,
            )
        return ProviderError(
            ProviderErrorCode.PROVIDER_UNAVAILABLE,
            f"Tripo is unavailable (HTTP {status}).",
            details=details,
        )

    def _data(self, response: httpx2.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise self._error(response)
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE, "Tripo returned an unreadable response."
            ) from exc
        if not isinstance(body, dict) or body.get("code") != 0:
            raise self._error(response)
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def _get(self, path: str) -> dict[str, Any]:
        try:
            return self._data(self._client.get(path))
        except httpx2.HTTPError as exc:
            raise network_error(NAME, exc) from exc

    def _upload(self, item: ProviderInput) -> str:
        filename = f"{item.role}{'.png' if item.mime_type == 'image/png' else '.jpg'}"
        try:
            with item.path.open("rb") as handle:
                data = self._data(
                    self._client.post("/files", files={"file": (filename, handle, item.mime_type)})
                )
        except httpx2.HTTPError as exc:
            raise network_error(NAME, exc) from exc
        token = data.get("file_token")
        if not isinstance(token, str) or not token:
            raise ProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE, "Tripo did not return a file token."
            )
        return token

    # -- Provider -----------------------------------------------------------

    def submit(self, request: GenerationRequest) -> str:
        tokens = [(item.role, self._upload(item)) for item in request.inputs]
        if request.kind == JobKind.IMAGE_TO_3D:
            path = "/generation/image-to-model"
            body: dict[str, Any] = {"input": tokens[0][1], **request.settings}
        else:
            path = "/generation/multiview-to-model"
            ordered = sorted(tokens, key=lambda t: VIEW_ORDER.index(t[0]))
            body = {"inputs": [{role: token} for role, token in ordered], **request.settings}
        try:
            response = self._client.post(path, json=body)
        except httpx2.ConnectError as exc:
            raise network_error(NAME, exc) from exc
        except httpx2.HTTPError as exc:
            # The request may have reached Tripo; resubmitting could create (and
            # charge) a second task, so this is never retried automatically.
            raise ProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "The connection to Tripo was lost while creating the task. It may have been "
                "created; check your Tripo dashboard before retrying.",
                retryable=False,
                details={"submission_uncertain": True},
            ) from exc
        data = self._data(response)
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE, "Tripo did not return a task id."
            )
        return task_id

    def poll(self, task_id: str) -> TaskStatus:
        data = self._get(f"/tasks/{task_id}")
        raw_status = str(data.get("status") or "unknown")
        # "unknown" (and anything new) is documented as non-terminal.
        state = _STATE.get(raw_status, "running")
        progress = data.get("progress")
        status = TaskStatus(
            state=state,
            progress=max(0.0, min(1.0, float(progress) / 100))
            if isinstance(progress, int | float)
            else None,
            raw={
                "status": raw_status,
                "progress": progress,
                "type": data.get("type"),
                "error_code": data.get("error_code"),
            },
        )
        credits = data.get("credits_consumed")
        if isinstance(credits, int | float):
            status.cost = {
                "credits": float(credits),
                "usd": round(float(credits) * USD_PER_CREDIT, 2),
                "source": "provider",
            }
        output = data.get("output")
        if isinstance(output, dict):
            for name, field in (("model", "model_url"), ("preview", "rendered_image_url")):
                if isinstance(output.get(field), str):
                    status.outputs[name] = output[field]
        if state == "failed":
            message = str(data.get("error_message") or "")
            if raw_status == "banned" or data.get("error_code") == _CODE_CONTENT_POLICY:
                code = ProviderErrorCode.CONTENT_REJECTED
                message = message or "Tripo's content policy rejected the image."
            elif raw_status == "expired":
                code = ProviderErrorCode.TASK_FAILED
                message = message or "The Tripo task expired."
            else:
                code = ProviderErrorCode.TASK_FAILED
                message = message or "Tripo reported that generation failed."
            status.error = ProviderError(
                code, message, retryable=False, details={"provider_code": data.get("error_code")}
            )
            status.message = message
        return status

    def cancel(self, task_id: str) -> CancelOutcome:
        return "unsupported"

    def download_result(self, task_id: str, status: TaskStatus, dest_dir: Path) -> ProviderResult:
        mesh_path = dest_dir / "model.glb"
        for attempt in range(2):
            url = status.outputs.get("model")
            if not url:
                raise ProviderError(
                    ProviderErrorCode.INVALID_OUTPUT, "Tripo did not return a model file."
                )
            try:
                download(
                    url,
                    mesh_path,
                    max_bytes=self._max_model_bytes,
                    transport=self._transport,
                    provider=NAME,
                )
                break
            except ProviderError as exc:
                # Links expire after 5 minutes; a fresh status carries a new one.
                if attempt == 0 and exc.details.get("expired_link"):
                    status = self.poll(task_id)
                    continue
                raise
        preview_path: Path | None = None
        if url := status.outputs.get("preview"):
            candidate = dest_dir / "preview"
            try:
                download(
                    url,
                    candidate,
                    max_bytes=PREVIEW_MAX_BYTES,
                    transport=self._transport,
                    provider=NAME,
                )
                preview_path = candidate
            except ProviderError:
                preview_path = None  # a preview is optional
        return ProviderResult(
            mesh_path=mesh_path,
            mesh_format="glb",
            preview_path=preview_path,
            metadata={"model_version": MODEL_VERSION},
        )

    def check_credentials(self) -> CredentialCheck:
        data = self._get("/account/balance")
        balance = data.get("balance")
        if isinstance(balance, int | float):
            frozen = data.get("frozen")
            extra = (
                f" ({frozen:g} reserved by running tasks)"
                if isinstance(frozen, int | float) and frozen
                else ""
            )
            return CredentialCheck(
                ok=True,
                message=f"Key accepted. Balance: {balance:g} credits{extra}.",
                balance=float(balance),
                unit="credits",
            )
        return CredentialCheck(ok=True, message="Key accepted.")

    def close(self) -> None:
        self._client.close()
