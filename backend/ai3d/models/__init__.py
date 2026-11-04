"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from ai3d.models.asset import Asset
from ai3d.models.device import LocalDevice
from ai3d.models.export import Export
from ai3d.models.image import Image
from ai3d.models.job import GenerationJob, JobEvent, JobInput
from ai3d.models.model3d import Model3D, ModelPart, ModelVersion
from ai3d.models.project import Project
from ai3d.models.provider import AIProvider, ProviderConfiguration
from ai3d.models.quality import QualityReport
from ai3d.models.setting import AppSetting

__all__ = [
    "AIProvider",
    "AppSetting",
    "Asset",
    "Export",
    "GenerationJob",
    "Image",
    "JobEvent",
    "JobInput",
    "LocalDevice",
    "Model3D",
    "ModelPart",
    "ModelVersion",
    "Project",
    "ProviderConfiguration",
    "QualityReport",
]
