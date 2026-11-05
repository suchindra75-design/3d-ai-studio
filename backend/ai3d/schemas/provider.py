from typing import Literal

from pydantic import Field

from ai3d.models.enums import CredentialSource, ProviderKind
from ai3d.schemas.common import Schema


class MultiViewLimitsRead(Schema):
    min_images: int
    max_images: int
    required_roles: list[str]


class ProviderCapabilitiesRead(Schema):
    single_image: bool
    multi_view: MultiViewLimitsRead | None
    accepted_mime_types: list[str]
    max_input_bytes: int
    output_formats: list[str]
    supports_texture: bool
    supports_pbr: bool
    cancel: Literal["supported", "pending_only", "unsupported"]
    reports_progress: bool
    notes: list[str]
    photogrammetry: MultiViewLimitsRead | None = None
    retexture: bool = False
    retexture_own_models_only: bool = False


class ProviderRead(Schema):
    key: str
    display_name: str
    kind: ProviderKind
    description: str
    uploads_images: bool = Field(description="True when generation sends photos to a third party.")
    homepage_url: str | None
    docs_url: str | None
    pricing_url: str | None
    requires_api_key: bool
    configured: bool
    credential_source: CredentialSource
    # Last four characters only. The key itself is never returned.
    api_key_hint: str | None
    api_key_env_var: str | None
    credential_error: str | None
    # Set when the engine cannot run here (e.g. COLMAP is not installed).
    unavailable_reason: str | None = None
    capabilities: ProviderCapabilitiesRead


class ProviderList(Schema):
    items: list[ProviderRead]


class ProviderKeyUpdate(Schema):
    api_key: str = Field(min_length=1, max_length=512, repr=False)


class CredentialCheckRead(Schema):
    ok: bool
    message: str
    balance: float | None = None
    unit: str | None = None
