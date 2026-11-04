"""Optional error reporting to a Sentry-compatible service.

Off unless ``AI3D_SENTRY_DSN`` is set and the ``errors`` extra is installed.
What is sent is deliberately narrow:

* only errors (log records at ERROR level, usually with a traceback) and a few
  warning breadcrumbs before them; no performance tracing, no sessions;
* no request data at all: no web-framework integration is enabled, so URLs,
  query strings (which can carry the desktop app's token), headers, cookies
  and bodies are never collected;
* no local variables, no source context, no host name, no user;
* tokens, keys and the home-folder path are scrubbed from every string.
"""

import logging
import re
from pathlib import Path
from typing import Any

import ai3d
from ai3d.core.config import Settings

log = logging.getLogger(__name__)

_SECRETS = re.compile(
    r"(access_token=|Bearer\s+|api[_-]?key[\"'=:\s]+|token[\"'=:\s]+)[^\s&\"',;]+",
    re.IGNORECASE,
)


def scrub(value: Any, home: str = str(Path.home())) -> Any:
    """Recursively removes secrets and the home-folder path from an event."""
    if isinstance(value, str):
        value = _SECRETS.sub(r"\1[removed]", value)
        return value.replace(home, "~") if home and home != "/" else value
    if isinstance(value, dict):
        return {key: scrub(item, home) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(item, home) for item in value]
    return value


def _before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    for key in ("request", "user", "server_name", "modules"):
        event.pop(key, None)
    return dict(scrub(event))


def _before_breadcrumb(crumb: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    return dict(scrub(crumb))


def setup_error_reporting(settings: Settings, *, transport: Any = None) -> bool:
    """Starts error reporting if configured. Returns True when it is on."""
    if settings.sentry_dsn is None:
        return False
    try:
        import sentry_sdk  # noqa: PLC0415 - optional dependency
        from sentry_sdk.integrations.dedupe import DedupeIntegration  # noqa: PLC0415
        from sentry_sdk.integrations.logging import LoggingIntegration  # noqa: PLC0415
        from sentry_sdk.integrations.threading import ThreadingIntegration  # noqa: PLC0415
    except ImportError:
        log.warning(
            "AI3D_SENTRY_DSN is set but sentry-sdk is not installed "
            "(uv sync --extra errors); error reporting stays off"
        )
        return False
    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        release=f"ai3d-studio@{ai3d.__version__}",
        environment=settings.environment,
        data_collection={
            "user_info": False,
            "cookies": {"mode": "off"},
            "http_headers": {"request": {"mode": "off"}},
            "http_bodies": [],
            "url_query_params": {"mode": "off"},
            "database_query_data": False,
            "queues": False,
            "stack_frame_variables": False,
            "frame_context_lines": 0,
        },
        server_name="",
        traces_sample_rate=0,
        auto_session_tracking=False,
        default_integrations=False,
        auto_enabling_integrations=False,
        integrations=[
            LoggingIntegration(level=logging.WARNING, event_level=logging.ERROR),
            DedupeIntegration(),
            ThreadingIntegration(),
        ],
        before_send=_before_send,  # type: ignore[arg-type]
        before_breadcrumb=_before_breadcrumb,
        transport=transport,
    )
    log.info("Error reporting is on (errors only, scrubbed)")
    return True
