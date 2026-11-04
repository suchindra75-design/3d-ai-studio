"""Local photogrammetry (COLMAP) as a selectable engine.

Registered like the cloud providers so planning, the settings screen and job
records treat every engine the same way, but it never uploads anything and it
does not use the submit/poll interface: the job worker runs the reconstruction
in-process (``workers/reconstruction.py``) on this machine.
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
from ai3d_engine.reconstruction import MIN_PHOTOS, check_colmap
from ai3d_engine.runtime import find_tool

if TYPE_CHECKING:
    from ai3d.core.config import Settings

MAX_PHOTOS = 300

# Quality profiles → COLMAP settings. Dense stereo is added at run time when a
# CUDA GPU is present (High quality and Professional only).
_SETTINGS: dict[QualityMode, dict[str, int | float | bool]] = {
    QualityMode.FAST: {
        "max_image_size": 1600,
        "max_num_features": 4096,
        "texture_scale": 0.5,
        "dense_if_cuda": False,
        # OpenMVS (CPU dense): depth maps at 1/4 of the photo size, half the triangles.
        "openmvs_resolution_level": 2,
        "openmvs_decimate": 0.5,
    },
    QualityMode.HIGH_QUALITY: {
        "max_image_size": 2400,
        "max_num_features": 8192,
        "texture_scale": 1.0,
        "dense_if_cuda": True,
        "openmvs_resolution_level": 1,
        "openmvs_decimate": 1.0,
    },
    QualityMode.PROFESSIONAL: {
        "max_image_size": 3200,
        "max_num_features": 12000,
        "texture_scale": 1.0,
        "dense_if_cuda": True,
        "openmvs_resolution_level": 1,
        "openmvs_decimate": 1.0,
    },
}

_SUMMARY: dict[QualityMode, tuple[str, ...]] = {
    QualityMode.FAST: (
        "Photos analysed at up to 1600 px",
        "Dense surface with OpenMVS (CPU) or an NVIDIA GPU, otherwise sparse",
        "Half-resolution texture",
    ),
    QualityMode.HIGH_QUALITY: (
        "Photos analysed at up to 2400 px",
        "Dense surface with OpenMVS (CPU) or an NVIDIA GPU, otherwise sparse",
        "Full-resolution texture",
    ),
    QualityMode.PROFESSIONAL: (
        "Photos analysed at up to 3200 px, more features",
        "Dense surface with OpenMVS (CPU) or an NVIDIA GPU, otherwise sparse",
        "Full-resolution texture",
    ),
}


class _RunsInProcessError(NotImplementedError):
    def __init__(self) -> None:
        super().__init__("Local photogrammetry runs in the job worker, not as a remote task.")


class LocalPhotogrammetryProvider(Provider):
    key: ClassVar[str] = "local-photogrammetry"
    display_name: ClassVar[str] = "Local photogrammetry (COLMAP)"
    kind: ClassVar[ProviderKind] = ProviderKind.LOCAL
    description: ClassVar[str] = (
        "Measures the object's shape from many overlapping photos on this computer. "
        "Nothing is uploaded. Free; needs COLMAP."
    )
    uploads_images: ClassVar[bool] = False
    homepage_url: ClassVar[str | None] = "https://colmap.github.io/"
    docs_url: ClassVar[str | None] = "https://colmap.github.io/tutorial.html"

    @classmethod
    def capabilities(cls) -> ProviderCapabilities:
        return ProviderCapabilities(
            single_image=False,
            multi_view=None,
            photogrammetry=MultiViewLimits(min_images=MIN_PHOTOS, max_images=MAX_PHOTOS),
            accepted_mime_types=("image/jpeg", "image/png", "image/webp"),
            max_input_bytes=0,  # photos are read from local storage, never uploaded
            output_formats=("glb",),
            supports_texture=True,
            supports_pbr=False,
            cancel="supported",
            reports_progress=True,
            notes=(
                "Scale is unknown: photogrammetry recovers shape, not size, without a "
                "reference object.",
                "A dense surface needs OpenMVS (runs on the CPU, several minutes) or an NVIDIA GPU "
                "with CUDA; otherwise it is built from sparse points, which is coarse on real "
                "photos.",
            ),
        )

    @classmethod
    def unavailable_reason(cls, settings: "Settings") -> str | None:
        if find_tool("colmap") is None:
            where = f" at {settings.colmap_path}" if settings.colmap_path else ""
            return (
                f"COLMAP was not found{where}. Install it (docs/LOCAL_GPU_SETUP.md) or set "
                "AI3D_COLMAP_PATH."
            )
        return check_colmap()

    @classmethod
    def plan(cls, kind: JobKind, quality_mode: QualityMode) -> ProviderPlan:
        return ProviderPlan(
            settings=dict(_SETTINGS[quality_mode]),
            summary=_SUMMARY[quality_mode],
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
        path = find_tool("colmap")
        if path is None:
            return CredentialCheck(ok=False, message="COLMAP was not found.")
        return CredentialCheck(ok=True, message=f"COLMAP found at {path}.")
