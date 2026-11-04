"""HTTP helpers shared by cloud adapters."""

import logging
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx2

import ai3d
from ai3d.core.timeutil import utcnow
from ai3d.providers.base import ProviderError, ProviderErrorCode

USER_AGENT = f"AI3DStudio/{ai3d.__version__}"
API_TIMEOUT = httpx2.Timeout(30.0, connect=10.0)
DOWNLOAD_TIMEOUT = httpx2.Timeout(120.0, connect=10.0)
_CHUNK = 1024 * 1024

# Request lines can contain signed download URLs; keep them out of the logs.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore2").setLevel(logging.WARNING)


def api_client(
    base_url: str, api_key: str, transport: httpx2.BaseTransport | None
) -> httpx2.Client:
    return httpx2.Client(
        base_url=base_url.rstrip("/"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        timeout=API_TIMEOUT,
        transport=transport,
        follow_redirects=False,
    )


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - utcnow()).total_seconds())


def network_error(provider: str, exc: httpx2.HTTPError) -> ProviderError:
    if isinstance(exc, httpx2.TimeoutException):
        return ProviderError(
            ProviderErrorCode.PROVIDER_UNAVAILABLE, f"{provider} did not respond in time."
        )
    return ProviderError(
        ProviderErrorCode.PROVIDER_UNAVAILABLE,
        f"Could not reach {provider}. Check the internet connection.",
    )


def download(
    url: str,
    dest: Path,
    *,
    max_bytes: int,
    transport: httpx2.BaseTransport | None,
    provider: str,
) -> int:
    """Stream ``url`` to ``dest``. The API key is never sent: result links are pre-signed."""
    if urlsplit(url).scheme != "https":
        raise ProviderError(
            ProviderErrorCode.INVALID_OUTPUT, f"{provider} returned a non-HTTPS download link."
        )
    size = 0
    try:
        with (
            httpx2.Client(
                headers={"User-Agent": USER_AGENT},
                timeout=DOWNLOAD_TIMEOUT,
                transport=transport,
                follow_redirects=True,
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code in {401, 403, 404, 410}:
                raise ProviderError(
                    ProviderErrorCode.TASK_FAILED,
                    f"The {provider} download link is no longer valid.",
                    retryable=True,
                    details={"http_status": response.status_code, "expired_link": True},
                )
            if response.status_code >= 400:
                raise ProviderError(
                    ProviderErrorCode.PROVIDER_UNAVAILABLE,
                    f"{provider} download failed (HTTP {response.status_code}).",
                )
            with dest.open("wb") as out:
                for chunk in response.iter_bytes(_CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ProviderError(
                            ProviderErrorCode.INVALID_OUTPUT,
                            f"The {provider} result is larger than the "
                            f"{max_bytes // (1024 * 1024)} MB limit.",
                            details={"max_model_bytes": max_bytes},
                        )
                    out.write(chunk)
    except httpx2.HTTPError as exc:
        raise network_error(provider, exc) from exc
    if size == 0:
        raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, f"{provider} returned an empty file.")
    return size
