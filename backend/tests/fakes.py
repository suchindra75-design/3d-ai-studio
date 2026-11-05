"""In-process fakes of the Tripo and Meshy HTTP APIs.

They follow the wire formats documented by each provider (see the adapter
modules) and record every request, so tests can check exactly what would be
sent. They are not recordings of real traffic and do not prove that a real
account works; that needs a manual run with a real key (docs/AI_PROVIDERS.md).
"""

import io
import json
from dataclasses import dataclass, field
from typing import Any

import httpx2
import trimesh
from PIL import Image

CDN = "https://cdn.example.test"


def glb_bytes() -> bytes:
    data: bytes = trimesh.creation.box(extents=(0.4, 0.3, 0.2)).export(file_type="glb")
    return data


def png_bytes() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 120, 40)).save(out, format="PNG")
    return out.getvalue()


@dataclass
class Recorded:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class FakeProviderServer:
    """Shared plumbing: routing, request log, CDN downloads."""

    requests: list[Recorded] = field(default_factory=list)
    model_file: bytes = field(default_factory=glb_bytes)
    preview_file: bytes = field(default_factory=png_bytes)
    # Number of status polls that report "running" before success.
    running_polls: int = 1
    # Override the response of a route: {(method, path_suffix): httpx2.Response}
    overrides: dict[tuple[str, str], httpx2.Response] = field(default_factory=dict)
    # CDN responses to serve before the real file (e.g. an expired link).
    cdn_failures: list[int] = field(default_factory=list)
    polls: int = 0

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def calls(self, method: str, suffix: str) -> list[Recorded]:
        return [
            r for r in self.requests if r.method == method and r.url.split("?")[0].endswith(suffix)
        ]

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = request.read()
        self.requests.append(
            Recorded(request.method, str(request.url), dict(request.headers), body)
        )
        url = str(request.url)
        if url.startswith(CDN):
            if self.cdn_failures:
                return httpx2.Response(self.cdn_failures.pop(0))
            if request.url.path.endswith(".glb"):
                return httpx2.Response(200, content=self.model_file)
            return httpx2.Response(200, content=self.preview_file)
        for (method, suffix), response in self.overrides.items():
            if request.method == method and request.url.path.endswith(suffix):
                return response
        return self.route(request, body)

    def route(self, request: httpx2.Request, body: bytes) -> httpx2.Response:
        raise NotImplementedError


class FakeTripo(FakeProviderServer):
    API = "https://openapi.tripo3d.ai/v3"

    def route(self, request: httpx2.Request, body: bytes) -> httpx2.Response:
        path = request.url.path.removeprefix("/v3")
        if request.headers.get("authorization") != "Bearer tsk_test_key_123456":
            return httpx2.Response(401, json={"code": 1002, "message": "Authentication failed"})
        if request.method == "POST" and path == "/files":
            index = len(self.calls("POST", "/files")) - 1
            return httpx2.Response(200, json={"code": 0, "data": {"file_token": f"file_{index}"}})
        if request.method == "POST" and path.startswith("/generation/"):
            return httpx2.Response(200, json={"code": 0, "data": {"task_id": "task-1"}})
        if request.method == "POST" and path == "/models/texture":
            # Documented 2026-11-04: {"input": task id | file token | URL, ...}.
            return httpx2.Response(200, json={"code": 0, "data": {"task_id": "task-1"}})
        if request.method == "GET" and path == "/tasks/task-1":
            self.polls += 1
            if self.polls <= self.running_polls:
                data: dict[str, Any] = {"task_id": "task-1", "status": "running", "progress": 40}
            else:
                data = {
                    "task_id": "task-1",
                    "type": "image_to_model",
                    "status": "success",
                    "progress": 100,
                    "credits_consumed": 40.0,
                    "output": {
                        "model_url": f"{CDN}/tripo/model.glb?sig={self.polls}",
                        "rendered_image_url": f"{CDN}/tripo/preview.png",
                    },
                }
            return httpx2.Response(200, json={"code": 0, "data": data})
        if request.method == "GET" and path == "/account/balance":
            return httpx2.Response(200, json={"code": 0, "data": {"balance": 1234.5, "frozen": 0}})
        return httpx2.Response(404, json={"code": 2001, "message": "not found"})


class FakeMeshy(FakeProviderServer):
    API = "https://api.meshy.ai/openapi/v1"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.deleted: list[str] = []
        self.pending = False

    def route(self, request: httpx2.Request, body: bytes) -> httpx2.Response:
        path = request.url.path.removeprefix("/openapi/v1")
        if request.headers.get("authorization") != "Bearer msy_test_key_123456":
            return httpx2.Response(401, json={"message": "Invalid API key"})
        if request.method == "POST" and path in {
            "/image-to-3d",
            "/multi-image-to-3d",
            "/retexture",
        }:
            return httpx2.Response(202, json={"result": "mesh-task-1"})
        if request.method == "GET" and path.endswith("/mesh-task-1"):
            self.polls += 1
            if self.pending:
                return httpx2.Response(
                    200, json={"id": "mesh-task-1", "status": "PENDING", "progress": 0}
                )
            if self.polls <= self.running_polls:
                return httpx2.Response(
                    200, json={"id": "mesh-task-1", "status": "IN_PROGRESS", "progress": 55}
                )
            return httpx2.Response(
                200,
                json={
                    "id": "mesh-task-1",
                    "status": "SUCCEEDED",
                    "progress": 100,
                    "model_urls": {"glb": f"{CDN}/meshy/model.glb?Signature=x"},
                    "thumbnail_url": f"{CDN}/meshy/preview.png",
                    "consumed_credits": 30,
                    "task_error": None,
                },
            )
        if request.method == "DELETE" and path.endswith("/mesh-task-1"):
            self.deleted.append(path)
            return httpx2.Response(200)
        if request.method == "GET" and path == "/balance":
            return httpx2.Response(200, json={"balance": 1000, "monthly_credits": 600})
        return httpx2.Response(404, json={"message": "Task not found"})
