"""Domain enumerations. Values are persisted as strings and exposed in the API."""

from enum import StrEnum


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class InputMode(StrEnum):
    """How the project's photos are meant to be used."""

    SINGLE_IMAGE = "single_image"  # one photo -> AI generation (unseen sides inferred)
    MULTI_VIEW = "multi_view"  # many photos -> AI multi-view or photogrammetry


class QualityMode(StrEnum):
    FAST = "fast"
    HIGH_QUALITY = "high_quality"
    PROFESSIONAL = "professional"


class ProcessingMode(StrEnum):
    CLOUD = "cloud"
    LOCAL = "local"
    AUTO = "auto"


class ImageKind(StrEnum):
    ORIGINAL = "original"  # byte-for-byte upload
    PREPROCESSED = "preprocessed"  # oriented/resized/EXIF-stripped copy
    SEGMENTED = "segmented"  # subject cut-out with alpha
    MASK = "mask"


class QualityLevel(StrEnum):
    GOOD = "good"
    WARNING = "warning"
    POOR = "poor"


class JobKind(StrEnum):
    IMAGE_TO_3D = "image_to_3d"
    MULTI_VIEW_TO_3D = "multi_view_to_3d"
    PHOTOGRAMMETRY = "photogrammetry"
    ENHANCE = "enhance"
    # A cloud service re-textures an existing version (the model is uploaded).
    RETEXTURE = "retexture"
    QUALITY_CHECK = "quality_check"
    EXPORT = "export"


class JobStatus(StrEnum):
    # The job would send photos to an external provider and is waiting for the
    # user's explicit permission. Nothing has left the machine yet.
    WAITING_FOR_CONSENT = "waiting_for_consent"
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}

    @property
    def is_active(self) -> bool:
        return self in {JobStatus.RUNNING, JobStatus.CANCELLING}


class JobEventType(StrEnum):
    STATUS = "status"
    STAGE = "stage"
    PROGRESS = "progress"
    LOG = "log"
    WARNING = "warning"
    ERROR = "error"


class ProviderKind(StrEnum):
    CLOUD_API = "cloud_api"
    LOCAL = "local"
    MOCK = "mock"  # automated tests only; never selectable in production


class CredentialSource(StrEnum):
    NONE = "none"
    ENVIRONMENT = "environment"
    STORED = "stored"  # encrypted at rest


class ModelVersionSource(StrEnum):
    GENERATED = "generated"
    RECONSTRUCTED = "reconstructed"
    ENHANCED = "enhanced"
    IMPORTED = "imported"
    EDITED = "edited"


class SurfaceProvenance(StrEnum):
    """Whether geometry was measured from photos or invented by a model."""

    MEASURED = "measured"
    INFERRED = "inferred"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class AssetKind(StrEnum):
    MESH = "mesh"
    TEXTURE = "texture"
    POINT_CLOUD = "point_cloud"
    PREVIEW_IMAGE = "preview_image"
    PROVIDER_RAW = "provider_raw"
    EXPORT_FILE = "export_file"
    REPORT = "report"


class ReportStatus(StrEnum):
    PASS = "pass"  # noqa: S105 - report status, not a password
    WARN = "warn"
    FAIL = "fail"


class ExportFormat(StrEnum):
    GLB = "glb"
    GLTF = "gltf"
    OBJ = "obj"
    STL = "stl"
    FBX = "fbx"
    USDZ = "usdz"
    THREE_MF = "3mf"


class ExportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
