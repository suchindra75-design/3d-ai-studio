"""Mock provider for automated tests.

It behaves like a cloud provider (it "uploads" photos, so the consent flow is
exercised) but returns a fixed placeholder sphere that has nothing to do with
the photos. It is registered only when ``AI3D_ENVIRONMENT=test`` and every
result it produces is labelled ``provider_key="mock"``.
"""

import io
import os
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

import trimesh
from PIL import Image, ImageDraw

from ai3d.models.enums import JobKind, ProviderKind, QualityMode
from ai3d.providers.base import (
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

_SUBDIVISIONS = {QualityMode.FAST: 2, QualityMode.HIGH_QUALITY: 3, QualityMode.PROFESSIONAL: 4}


@dataclass
class MockScenario:
    """How the next mock tasks behave. Tests replace ``MockProvider.scenario``.

    Tests that run the server as a separate process configure it with environment
    variables: ``AI3D_MOCK_POLLS_UNTIL_DONE`` makes tasks last longer (to cancel one or
    stop the server in the middle of one), and ``AI3D_MOCK_FAIL_FIRST_TASK=1`` makes the
    first task for each set of photos fail, so retrying it succeeds.
    """

    polls_until_done: int = field(
        default_factory=lambda: int(os.environ.get("AI3D_MOCK_POLLS_UNTIL_DONE", "2"))
    )
    fail_on: Literal["submit", "poll"] | None = None
    error_code: ProviderErrorCode = ProviderErrorCode.TASK_FAILED
    retry_after: float | None = None
    cancel_outcome: CancelOutcome = "cancelled"
    output: Literal["sphere", "garbage"] = "sphere"
    fail_first_task: bool = field(
        default_factory=lambda: os.environ.get("AI3D_MOCK_FAIL_FIRST_TASK") == "1"
    )


@dataclass
class MockSubmission:
    task_id: str
    kind: JobKind
    settings: dict[str, object]
    roles: list[str]
    input_bytes: list[bytes] = field(default_factory=list)


class MockProvider(Provider):
    key = "mock"
    display_name = "Mock provider (tests only)"
    kind = ProviderKind.MOCK
    uploads_images = True
    description = (
        "Simulated cloud provider for automated tests. Returns a placeholder sphere, "
        "never a reconstruction of your photos."
    )

    scenario: ClassVar[MockScenario] = MockScenario()
    submissions: ClassVar[list[MockSubmission]] = []
    _polls: ClassVar[dict[str, int]] = {}
    _cancelled: ClassVar[set[str]] = set()
    _doomed: ClassVar[set[str]] = set()  # tasks that will fail (fail_first_task)
    _seen_inputs: ClassVar[set[tuple[str, ...]]] = set()
    _lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls.scenario = MockScenario()
            cls.submissions = []
            cls._polls = {}
            cls._cancelled = set()
            cls._doomed = set()
            cls._seen_inputs = set()

    @classmethod
    def capabilities(cls) -> ProviderCapabilities:
        return ProviderCapabilities(
            single_image=True,
            multi_view=MultiViewLimits(min_images=2, max_images=4, required_roles=("front",)),
            accepted_mime_types=("image/jpeg", "image/png"),
            max_input_bytes=20 * 1024 * 1024,
            output_formats=("glb",),
            supports_texture=False,
            supports_pbr=False,
            cancel="supported",
            reports_progress=True,
            notes=("Test provider: output is a placeholder sphere.",),
        )

    @classmethod
    def plan(cls, kind: JobKind, quality_mode: QualityMode) -> ProviderPlan:
        return ProviderPlan(
            settings={"shape": "icosphere", "subdivisions": _SUBDIVISIONS[quality_mode]},
            summary=("Placeholder sphere (test provider)",),
            estimate=CostEstimate(amount=0, unit="credits", usd=0.0, basis="Test provider: free."),
        )

    def _fail(self) -> ProviderError:
        return ProviderError(
            self.scenario.error_code,
            f"Mock provider simulated {self.scenario.error_code.value}.",
            retry_after=self.scenario.retry_after,
        )

    def submit(self, request: GenerationRequest) -> str:
        if self.scenario.fail_on == "submit":
            raise self._fail()
        task_id = f"mock-{uuid.uuid4().hex}"
        photos = tuple(sorted(item.sha256 for item in request.inputs))
        with self._lock:
            if self.scenario.fail_first_task and photos not in self._seen_inputs:
                self._doomed.add(task_id)
            self._seen_inputs.add(photos)
            self.submissions.append(
                MockSubmission(
                    task_id=task_id,
                    kind=request.kind,
                    settings=dict(request.settings),
                    roles=[item.role for item in request.inputs],
                    input_bytes=[item.path.read_bytes() for item in request.inputs],
                )
            )
        return task_id

    def poll(self, task_id: str) -> TaskStatus:
        with self._lock:
            if task_id in self._cancelled:
                return TaskStatus(state="cancelled")
            count = self._polls.get(task_id, 0) + 1
            self._polls[task_id] = count
        steps = max(1, self.scenario.polls_until_done)
        if count < steps:
            return TaskStatus(state="running", progress=count / steps)
        if self.scenario.fail_on == "poll" or task_id in self._doomed:
            error = self._fail()
            error.retryable = False
            return TaskStatus(state="failed", error=error, message=error.message)
        return TaskStatus(
            state="succeeded",
            progress=1.0,
            cost={"credits": 0.0, "usd": 0.0, "source": "provider"},
            outputs={"model": f"mock://{task_id}/model.glb"},
        )

    def cancel(self, task_id: str) -> CancelOutcome:
        outcome = self.scenario.cancel_outcome
        if outcome == "cancelled":
            with self._lock:
                self._cancelled.add(task_id)
        return outcome

    def download_result(self, task_id: str, status: TaskStatus, dest_dir: Path) -> ProviderResult:
        mesh_path = dest_dir / "model.glb"
        if self.scenario.output == "garbage":
            mesh_path.write_bytes(b"this is not a glb file")
        else:
            subdivisions = 3
            for submission in self.submissions:
                if submission.task_id == task_id:
                    subdivisions = int(submission.settings.get("subdivisions", 3))  # type: ignore[call-overload]
            sphere = trimesh.creation.icosphere(subdivisions=subdivisions, radius=0.5)
            mesh_path.write_bytes(sphere.export(file_type="glb"))
        preview_path = dest_dir / "preview.png"
        preview_path.write_bytes(_preview_png())
        return ProviderResult(
            mesh_path=mesh_path,
            mesh_format="glb",
            preview_path=preview_path,
            metadata={"mock": True},
        )

    def check_credentials(self) -> CredentialCheck:
        return CredentialCheck(ok=True, message="The test provider needs no key.")


def _preview_png() -> bytes:
    image = Image.new("RGB", (256, 256), (24, 26, 32))
    draw = ImageDraw.Draw(image)
    draw.ellipse((48, 48, 208, 208), fill=(110, 116, 132))
    draw.text((104, 122), "MOCK", fill=(240, 240, 240))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
