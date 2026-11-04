from fastapi import APIRouter
from sqlalchemy.orm import Session

from ai3d.api.deps import DbDep, ProvidersDep
from ai3d.schemas.provider import (
    CredentialCheckRead,
    MultiViewLimitsRead,
    ProviderCapabilitiesRead,
    ProviderKeyUpdate,
    ProviderList,
    ProviderRead,
)
from ai3d.services.providers import ProviderManager

router = APIRouter(prefix="/providers", tags=["providers"])


def _read(db: Session, manager: ProviderManager, key: str) -> ProviderRead:
    cls = manager.provider_class(key)
    credential = manager.credential(db, key)
    caps = cls.capabilities()
    return ProviderRead(
        key=cls.key,
        display_name=cls.display_name,
        kind=cls.kind,
        description=cls.description,
        uploads_images=cls.uploads_images,
        homepage_url=cls.homepage_url,
        docs_url=cls.docs_url,
        pricing_url=cls.pricing_url,
        requires_api_key=cls.api_key_setting is not None,
        configured=manager.is_configured(db, key),
        credential_source=credential.source,
        api_key_hint=credential.hint,
        api_key_env_var=f"AI3D_{cls.api_key_setting.upper()}" if cls.api_key_setting else None,
        credential_error=credential.error,
        unavailable_reason=manager.unavailable_reason(key),
        capabilities=ProviderCapabilitiesRead(
            single_image=caps.single_image,
            multi_view=MultiViewLimitsRead(
                min_images=caps.multi_view.min_images,
                max_images=caps.multi_view.max_images,
                required_roles=list(caps.multi_view.required_roles),
            )
            if caps.multi_view
            else None,
            accepted_mime_types=list(caps.accepted_mime_types),
            max_input_bytes=caps.max_input_bytes,
            output_formats=list(caps.output_formats),
            supports_texture=caps.supports_texture,
            supports_pbr=caps.supports_pbr,
            cancel=caps.cancel,
            reports_progress=caps.reports_progress,
            notes=list(caps.notes),
            photogrammetry=MultiViewLimitsRead(
                min_images=caps.photogrammetry.min_images,
                max_images=caps.photogrammetry.max_images,
                required_roles=[],
            )
            if caps.photogrammetry
            else None,
        ),
    )


@router.get("", response_model=ProviderList, summary="List AI providers and their configuration")
def list_providers(db: DbDep, manager: ProvidersDep) -> ProviderList:
    return ProviderList(items=[_read(db, manager, cls.key) for cls in manager.registry.all()])


@router.put(
    "/{key}/api-key",
    response_model=ProviderRead,
    summary="Store an API key (encrypted at rest; never returned)",
)
def set_api_key(
    db: DbDep, manager: ProvidersDep, key: str, body: ProviderKeyUpdate
) -> ProviderRead:
    manager.store_key(db, key, body.api_key)
    return _read(db, manager, key)


@router.delete("/{key}/api-key", response_model=ProviderRead, summary="Remove the stored API key")
def delete_api_key(db: DbDep, manager: ProvidersDep, key: str) -> ProviderRead:
    manager.delete_key(db, key)
    return _read(db, manager, key)


@router.post(
    "/{key}/check",
    response_model=CredentialCheckRead,
    summary="Verify the key with a free read-only call (account balance)",
)
def check_provider(db: DbDep, manager: ProvidersDep, key: str) -> CredentialCheckRead:
    result = manager.check(db, key)
    return CredentialCheckRead(
        ok=result.ok, message=result.message, balance=result.balance, unit=result.unit
    )
