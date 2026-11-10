"""Local single-image AI generation (TripoSR) as a selectable engine.

Registered like the other providers so planning, settings and job records
treat it the same way, but, like local photogrammetry, it runs in the job
worker on this computer (``workers/local_generation.py``): nothing is
uploaded and there is no remote task to submit or poll.
"""

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from ai3d.models.enums import JobKind, ProviderKind, QualityMode
from ai3d.providers.base import (
    CancelOutcome,
    CostEstimate,
    CredentialCheck,
    GenerationRequest,
    MultiViewLimits,
    Provider,
    ProviderCapabilities,
    ProviderPlan,
    ProviderResult,
    TaskStatus,
)
from ai3d_engine.local_ai import check_local_ai, status

if TYPE_CHECKING:
    from ai3d.core.config import Settings

# Quality profiles → marching-cubes grid. Higher grids give finer surfaces and
# take roughly cubically longer to extract (256: ~35 s on a 6-core Intel CPU).
_RESOLUTION: dict[QualityMode, int] = {
    QualityMode.FAST: 160,
    QualityMode.HIGH_QUALITY: 256,
    QualityMode.PROFESSIONAL: 320,
}
# Refinement (processing/refine.py): triangles kept and texture size per profile.
_REFINE: dict[QualityMode, tuple[int, int]] = {
    QualityMode.FAST: (20_000, 1024),
    QualityMode.HIGH_QUALITY: (40_000, 2048),
    QualityMode.PROFESSIONAL: (80_000, 2048),
}


class _RunsInProcessError(NotImplementedError):
    def __init__(self) -> None:
        super().__init__("Local AI generation runs in the job worker, not as a remote task.")


class LocalAIProvider(Provider):
    key: ClassVar[str] = "local-triposr"
    display_name: ClassVar[str] = "Local AI (TripoSR)"
    kind: ClassVar[ProviderKind] = ProviderKind.LOCAL
    description: ClassVar[str] = (
        "Generates a 3D model from one photo on this computer with the open TripoSR model "
        "(MIT licence). Nothing is uploaded. Free; works on a CPU (about 1–3 minutes), "
        "faster with an NVIDIA GPU."
    )
    uploads_images: ClassVar[bool] = False
    homepage_url: ClassVar[str | None] = "https://github.com/VAST-AI-Research/TripoSR"
    docs_url: ClassVar[str | None] = "https://huggingface.co/stabilityai/TripoSR"

    @classmethod
    def capabilities(cls) -> ProviderCapabilities:
        return ProviderCapabilities(
            single_image=True,
            # The shape comes from the front photo; the other views correct its depth
            # and colour their sides (processing/refine.py).
            multi_view=MultiViewLimits(min_images=2, max_images=4, required_roles=("front",)),
            accepted_mime_types=("image/jpeg", "image/png", "image/webp"),
            # Limit for the metadata-free working copy; it is never uploaded.
            max_input_bytes=50 * 1024 * 1024,
            output_formats=("glb",),
            supports_texture=True,  # baked by processing/refine.py
            supports_pbr=False,
            cancel="supported",
            reports_progress=True,
            notes=(
                "AI-generated from one photo: the sides the camera did not see are invented.",
                "Colours are baked into a texture; the front uses the photo itself, "
                "the other sides the model's own colours. Size is not measured.",
                "The background is removed automatically; one clearly visible object works best.",
            ),
        )

    @classmethod
    def unavailable_reason(cls, settings: "Settings") -> str | None:
        return check_local_ai()

    @classmethod
    def plan(cls, kind: JobKind, quality_mode: QualityMode) -> ProviderPlan:
        resolution = _RESOLUTION[quality_mode]
        triangles, texture = _REFINE[quality_mode]
        views = (
            (
                "Shape from the front photo; the other views correct its depth and colour "
                "their sides",
            )
            if kind == JobKind.MULTI_VIEW_TO_3D
            else ()
        )
        return ProviderPlan(
            settings={
                "resolution": resolution,
                "refine": {"triangles": triangles, "texture_size": texture},
            },
            summary=(
                "Background removed on this computer",
                *views,
                f"Surface extracted on a {resolution}³ grid",
                f"Cleaned up and simplified to about {triangles:,} triangles",
                f"Colours baked into a {texture}×{texture} px texture, "
                "with the photo projected onto the front",
            ),
            estimate=CostEstimate(
                amount=0, unit="USD", usd=0.0, basis="Runs on this computer; nothing is paid."
            ),
        )

    def submit(self, request: GenerationRequest) -> str:
        raise _RunsInProcessError

    def poll(self, task_id: str) -> TaskStatus:
        raise _RunsInProcessError

    def cancel(self, task_id: str) -> CancelOutcome:
        raise _RunsInProcessError

    def download_result(self, task_id: str, status: TaskStatus, dest_dir: Path) -> ProviderResult:
        raise _RunsInProcessError

    def check_credentials(self) -> CredentialCheck:
        state = status()
        if not state.installed:
            return CredentialCheck(ok=False, message=state.problem or "Not installed.")
        return CredentialCheck(ok=True, message=f"Local AI engine installed at {state.path}.")
