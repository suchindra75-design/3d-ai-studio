"""Your own GPU server (``gpu-server/``): TRELLIS on an NVIDIA GPU you run.

The server is this project's own code, deployed on a machine you rent or own
(docs/GPU_SERVER.md). Photos go to that machine and nowhere else; generation is
paid by the hour of server time, not per model. The result is a coloured mesh
and each photo framed as the model saw it; the app then cleans up and textures
the model on this computer (``processing/refine.py``) like local AI models.

API (version 1, every request with ``Authorization: Bearer <token>``):

* ``GET /v1/health`` → backend, model, device, queue
* ``POST /v1/jobs`` multipart ``photos`` + ``roles`` (front first) + ``settings`` JSON → job
* ``GET /v1/jobs/{id}`` → ``status`` queued|running|succeeded|failed|cancelled,
  ``progress`` 0..1, ``message``, ``error``, ``files``
* ``POST /v1/jobs/{id}/cancel``; ``DELETE /v1/jobs/{id}`` (finished jobs)
* ``GET /v1/jobs/{id}/files/{name}``: ``model.glb``, ``cutout-<role>.png``, ``result.json``
"""

import contextlib
import json
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

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
    ProviderPlan,
    ProviderResult,
    TaskStatus,
)
from ai3d.providers.http import API_TIMEOUT, DOWNLOAD_TIMEOUT, USER_AGENT, network_error

NAME = "your GPU server"
MAX_INPUT_BYTES = 20 * 1024 * 1024  # the server's own limit per photo
CUTOUT_MAX_BYTES = 30 * 1024 * 1024
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_CHUNK = 1024 * 1024

# Sampling steps per profile (TRELLIS-image-large's own default is 25) and what the
# app's refinement keeps (as for local AI).
_STEPS: dict[QualityMode, int] = {
    QualityMode.FAST: 12,
    QualityMode.HIGH_QUALITY: 25,
    QualityMode.PROFESSIONAL: 50,
}
_REFINE: dict[QualityMode, tuple[int, int]] = {
    QualityMode.FAST: (30_000, 1024),
    QualityMode.HIGH_QUALITY: (60_000, 2048),
    QualityMode.PROFESSIONAL: (120_000, 2048),
}
_STATES = {"queued", "running", "succeeded", "failed", "cancelled"}


def check_server_url(url: str) -> str:
    """The server address, normalised; refuses addresses the token must not go to."""
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise ValueError("Enter the server's address, e.g. https://gpu.example.com:8800.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Enter only the server's address (no user name, query or #).")
    if parts.scheme == "http" and parts.hostname not in LOCAL_HOSTS:
        raise ValueError(
            "Use https:// (the access token is sent with every request). Plain http:// is "
            "accepted only for this computer, e.g. an SSH tunnel to the server."
        )
    return url


class GpuServerProvider(Provider):
    key: ClassVar[str] = "gpu-server"
    display_name: ClassVar[str] = "Your GPU server (TRELLIS)"
    kind: ClassVar[ProviderKind] = ProviderKind.CLOUD_API
    description: ClassVar[str] = (
        "Generates on an NVIDIA GPU machine you run, with Microsoft's TRELLIS model (MIT "
        "licence): sharper shapes and believable backs, from one photo or up to four views. "
        "Photos go only to your server; the app then textures the model on this computer."
    )
    uploads_images: ClassVar[bool] = True  # to your own server, not a third party
    homepage_url: ClassVar[str | None] = "https://github.com/microsoft/TRELLIS"
    docs_url: ClassVar[str | None] = None
    api_key_setting: ClassVar[str | None] = "gpu_server_token"
    base_url_setting: ClassVar[str | None] = "gpu_server_url"
    refines_output: ClassVar[bool] = True

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None = None,
        transport: httpx2.BaseTransport | None = None,
        max_model_bytes: int = DEFAULT_MAX_MODEL_BYTES,
    ) -> None:
        super().__init__(
            api_key=api_key, base_url=base_url, transport=transport, max_model_bytes=max_model_bytes
        )
        if not api_key:
            raise ProviderError(
                ProviderErrorCode.AUTH_FAILED, "No access token for the GPU server."
            )
        if not base_url:
            raise ProviderError(
                ProviderErrorCode.INVALID_INPUT, "Set the GPU server's address in Settings."
            )
        try:
            base = check_server_url(base_url)
        except ValueError as exc:
            raise ProviderError(ProviderErrorCode.INVALID_INPUT, str(exc)) from exc
        self._client = httpx2.Client(
            base_url=base,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT},
            timeout=API_TIMEOUT,
            transport=transport,
            follow_redirects=False,
        )

    @classmethod
    def capabilities(cls) -> ProviderCapabilities:
        return ProviderCapabilities(
            single_image=True,
            multi_view=MultiViewLimits(min_images=2, max_images=4, required_roles=("front",)),
            accepted_mime_types=("image/jpeg", "image/png", "image/webp"),
            max_input_bytes=MAX_INPUT_BYTES,
            output_formats=("glb",),
            supports_texture=True,  # baked on this computer (processing/refine.py)
            supports_pbr=False,
            cancel="supported",
            reports_progress=True,
            notes=(
                "Runs on your own GPU server: the photos are sent to it and nowhere else.",
                "AI-generated: parts no photo shows are invented, and size is not measured.",
                "The app removes stray pieces, simplifies the surface and bakes a texture "
                "from the photos on this computer.",
            ),
        )

    @classmethod
    def plan(cls, kind: JobKind, quality_mode: QualityMode) -> ProviderPlan:
        steps = _STEPS[quality_mode]
        triangles, texture = _REFINE[quality_mode]
        views = (
            ("All views shape the model together (TRELLIS multi-image)",)
            if kind == JobKind.MULTI_VIEW_TO_3D
            else ()
        )
        return ProviderPlan(
            settings={
                "generation": {"structure_steps": steps, "detail_steps": steps},
                "refine": {"triangles": triangles, "texture_size": texture},
            },
            summary=(
                "Background removed on your GPU server",
                *views,
                f"Shape generated by TRELLIS in {steps} + {steps} steps",
                f"Cleaned up and simplified to about {triangles:,} triangles on this computer",
                f"Colours baked into a {texture}×{texture} px texture, with the photos "
                "projected on",
            ),
            estimate=CostEstimate(
                amount=0,
                unit="USD",
                usd=None,
                basis="Runs on your GPU server; you pay its rental by the hour, not per model.",
            ),
        )

    # -- HTTP --------------------------------------------------------------------

    def _error(self, response: httpx2.Response, doing: str) -> ProviderError:
        try:
            detail = str(response.json().get("detail") or "")
        except ValueError:
            detail = ""
        status = response.status_code
        details = {"http_status": status}
        if status == 401:
            return ProviderError(
                ProviderErrorCode.AUTH_FAILED,
                "The GPU server refused the access token. Check it in Settings.",
                details=details,
            )
        if status in {413, 422}:
            return ProviderError(
                ProviderErrorCode.INVALID_INPUT,
                detail or "The GPU server refused the photos.",
                details=details,
            )
        if status == 503:
            return ProviderError(
                ProviderErrorCode.RATE_LIMITED, detail or "The GPU server is busy.", details=details
            )
        said = f": {detail}" if detail else ""
        return ProviderError(
            ProviderErrorCode.PROVIDER_UNAVAILABLE,
            f"The GPU server failed while {doing} (HTTP {status}{said}).",
            details=details,
        )

    def _json(self, response: httpx2.Response, doing: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise self._error(response, doing)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                ProviderErrorCode.INVALID_OUTPUT, "The GPU server sent an unreadable answer."
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(
                ProviderErrorCode.INVALID_OUTPUT, "The GPU server sent an unexpected answer."
            )
        return data

    def _fetch(self, path: str, dest: Path, max_bytes: int) -> int:
        size = 0
        try:
            with self._client.stream("GET", path, timeout=DOWNLOAD_TIMEOUT) as response:
                if response.status_code >= 400:
                    response.read()
                    raise self._error(response, "sending the result")
                with dest.open("wb") as out:
                    for chunk in response.iter_bytes(_CHUNK):
                        size += len(chunk)
                        if size > max_bytes:
                            raise ProviderError(
                                ProviderErrorCode.INVALID_OUTPUT,
                                f"The GPU server's result is larger than "
                                f"{max_bytes // (1024 * 1024)} MB.",
                            )
                        out.write(chunk)
        except httpx2.HTTPError as exc:
            raise network_error(NAME, exc) from exc
        if size == 0:
            raise ProviderError(
                ProviderErrorCode.INVALID_OUTPUT, "The GPU server sent an empty file."
            )
        return size

    # -- provider ----------------------------------------------------------------

    def submit(self, request: GenerationRequest) -> str:
        ordered = sorted(
            request.inputs,
            key=lambda item: VIEW_ORDER.index(item.role) if item.role in VIEW_ORDER else -1,
        )
        roles = ["source"] if len(ordered) == 1 else [item.role for item in ordered]
        settings = dict(request.settings.get("generation") or {})
        files = [
            ("photos", (item.path.name, item.path.read_bytes(), item.mime_type)) for item in ordered
        ]
        try:
            response = self._client.post(
                "/v1/jobs",
                files=files,
                data={"roles": ",".join(roles), "settings": json.dumps(settings)},
                timeout=DOWNLOAD_TIMEOUT,
            )
        except httpx2.ConnectError as exc:
            raise network_error(NAME, exc) from exc
        except httpx2.HTTPError as exc:
            raise ProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "The connection to the GPU server was lost while sending the photos.",
                retryable=False,
                details={"submission_uncertain": True},
            ) from exc
        data = self._json(response, "receiving the photos")
        job_id = data.get("id")
        if not isinstance(job_id, str) or not job_id.isalnum():
            raise ProviderError(
                ProviderErrorCode.INVALID_OUTPUT, "The GPU server returned no job id."
            )
        return job_id

    def poll(self, task_id: str) -> TaskStatus:
        try:
            data = self._json(self._client.get(f"/v1/jobs/{task_id}"), "reporting on the job")
        except httpx2.HTTPError as exc:
            raise network_error(NAME, exc) from exc
        state = data.get("status")
        if state not in _STATES:
            raise ProviderError(
                ProviderErrorCode.INVALID_OUTPUT, "The GPU server reported an unknown status."
            )
        progress = data.get("progress")
        status = TaskStatus(
            state=state,
            progress=float(progress) if isinstance(progress, int | float) else None,
            message=str(data.get("message") or ""),
            raw={"stage": data.get("stage"), "files": data.get("files")},
        )
        if state == "failed":
            code = (
                ProviderErrorCode.INVALID_INPUT
                if data.get("error") == "no_object"
                else ProviderErrorCode.TASK_FAILED
            )
            status.error = ProviderError(
                code, status.message or "The GPU job failed.", retryable=False
            )
        return status

    def cancel(self, task_id: str) -> CancelOutcome:
        try:
            data = self._json(self._client.post(f"/v1/jobs/{task_id}/cancel"), "cancelling")
        except httpx2.HTTPError as exc:
            raise network_error(NAME, exc) from exc
        # A running job stops at its next step; a finished one cannot be cancelled.
        return "too_late" if data.get("status") == "succeeded" else "cancelled"

    def download_result(self, task_id: str, status: TaskStatus, dest_dir: Path) -> ProviderResult:
        mesh_path = dest_dir / "model.glb"
        self._fetch(f"/v1/jobs/{task_id}/files/model.glb", mesh_path, self._max_model_bytes)
        result_path = dest_dir / "result.json"
        self._fetch(f"/v1/jobs/{task_id}/files/result.json", result_path, 1024 * 1024)
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ProviderError(
                ProviderErrorCode.INVALID_OUTPUT, "The GPU server's result.json is unreadable."
            ) from exc
        views = []
        for view in result.get("views", []):
            role, name = view.get("role"), view.get("cutout")
            if role not in ("source", *VIEW_ORDER) or name != f"cutout-{role}.png":
                continue
            path = dest_dir / name
            self._fetch(f"/v1/jobs/{task_id}/files/{name}", path, CUTOUT_MAX_BYTES)
            views.append({"role": role, "cutout": str(path), "crop_side": view.get("crop_side")})
        with contextlib.suppress(httpx2.HTTPError):  # the server keeps nothing it need not
            self._client.delete(f"/v1/jobs/{task_id}")
        info = {
            k: result.get(k) for k in ("model", "device", "seconds", "seed", "triangles", "views")
        }
        return ProviderResult(
            mesh_path=mesh_path,
            mesh_format="glb",
            metadata={"gpu_server": info, "cutouts": views},
        )

    def check_credentials(self) -> CredentialCheck:
        try:
            data = self._json(self._client.get("/v1/health"), "answering")
        except httpx2.HTTPError as exc:
            raise network_error(NAME, exc) from exc
        waiting = int(data.get("queued") or 0) + int(data.get("running") or 0)
        return CredentialCheck(
            ok=True,
            message=(
                f"Connected: {data.get('backend')} ({data.get('model')}) on {data.get('device')}; "
                f"{waiting} job{'s' if waiting != 1 else ''} in progress."
            ),
        )

    def close(self) -> None:
        self._client.close()
