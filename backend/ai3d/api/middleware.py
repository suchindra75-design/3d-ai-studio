"""Pure ASGI middleware (safe for streaming responses such as SSE)."""

import hmac
import json
import logging
import re
import time
import uuid
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai3d.core.logging import request_id_var

log = logging.getLogger("ai3d.request")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RequestContextMiddleware:
    """Assigns/propagates ``X-Request-ID`` and logs one line per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            log.info(
                "%s %s %s",
                scope["method"],
                scope["path"],
                status_code,
                extra={"duration_ms": round((time.perf_counter() - started) * 1000, 1)},
            )
            request_id_var.reset(token)


class BodySizeLimitMiddleware:
    """Rejects request bodies larger than ``max_bytes`` before they are spooled to disk."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def _reject(self, send: Send) -> None:
        body = (
            b'{"error":{"code":"payload_too_large","message":"Request body is too large.",'
            b'"details":{},"request_id":null}}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > self.max_bytes:
            await self._reject(send)
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLargeError:
            if not response_started:
                await self._reject(send)


class _BodyTooLargeError(Exception):
    pass


class ApiTokenMiddleware:
    """Requires a shared secret on every API request (desktop app).

    The desktop shell starts the backend on a random local port with a fresh
    token and gives it only to its own window. Other programs and web pages on
    the machine can reach 127.0.0.1 too; without the token they get 401. The
    token is accepted as ``Authorization: Bearer …`` or, for URLs the browser
    loads by itself (images, downloads, event streams), as ``?access_token=…``.
    Request logs record the path only, never the query string.
    """

    OPEN_PATHS = frozenset({"/api/v1/health"})

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token.encode()

    def _presented(self, scope: Scope) -> bytes | None:
        headers: dict[bytes, bytes] = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"")
        if auth[:7].lower() == b"bearer ":
            return auth[7:].strip()
        query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
        values = query.get("access_token")
        return values[0].encode("latin-1") if values else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] == "OPTIONS"
            or not scope["path"].startswith("/api/")
            or scope["path"] in self.OPEN_PATHS
        ):
            await self.app(scope, receive, send)
            return
        presented = self._presented(scope)
        if presented is not None and hmac.compare_digest(presented, self.token):
            await self.app(scope, receive, send)
            return
        body = json.dumps(
            {
                "error": {
                    "code": "unauthorized",
                    "message": "This backend only answers the AI 3D Studio app that started it.",
                    "details": {},
                    "request_id": None,
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
