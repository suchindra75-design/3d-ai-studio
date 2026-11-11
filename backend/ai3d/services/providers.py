"""Provider configuration and credentials.

Keys are resolved in this order: environment (``AI3D_<PROVIDER>_API_KEY``),
then a key stored encrypted through the API. Plain keys never leave this
module except to construct an adapter; the API exposes only a short hint.
"""

import logging
import uuid
from dataclasses import dataclass

import httpx2
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai3d.core.config import Settings
from ai3d.core.errors import AppError, InvalidInputError, NotFoundError
from ai3d.core.secrets import SecretBox, SecretDecryptionError, secret_hint
from ai3d.models import AIProvider, ProviderConfiguration
from ai3d.models.enums import CredentialSource
from ai3d.providers import Provider, ProviderRegistry
from ai3d.providers.base import CredentialCheck, ProviderError
from ai3d.providers.gpu_server import check_server_url

log = logging.getLogger(__name__)

MAX_KEY_LENGTH = 512


class ProviderNotConfiguredError(AppError):
    status_code = 409
    code = "provider_not_configured"


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    source: CredentialSource
    api_key: str | None = None
    hint: str | None = None
    config_id: uuid.UUID | None = None
    # Set when a stored key exists but cannot be used.
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.api_key is not None


class ProviderManager:
    def __init__(
        self,
        settings: Settings,
        registry: ProviderRegistry,
        secrets: SecretBox,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.secrets = secrets
        # Injected by tests to replay recorded provider responses.
        self.transport = transport

    # -- registry rows --------------------------------------------------------

    def sync(self, db: Session) -> None:
        """Make sure every registered provider has an ``ai_providers`` row."""
        existing = {row.key: row for row in db.scalars(select(AIProvider))}
        for cls in self.registry.all():
            row = existing.get(cls.key) or AIProvider(key=cls.key)
            row.display_name = cls.display_name
            row.kind = cls.kind
            row.description = cls.description
            row.homepage_url = cls.homepage_url
            row.capabilities = {"uploads_images": cls.uploads_images}
            db.add(row)
        db.commit()

    def provider_class(self, key: str) -> type[Provider]:
        cls = self.registry.get(key)
        if cls is None:
            raise NotFoundError(f"Unknown provider {key!r}.", details={"provider": key})
        return cls

    def _provider_row(self, db: Session, key: str) -> AIProvider:
        row = db.scalar(select(AIProvider).where(AIProvider.key == key))
        if row is None:
            self.sync(db)
            row = db.scalar(select(AIProvider).where(AIProvider.key == key))
        assert row is not None
        return row

    def _default_config(self, db: Session, key: str) -> ProviderConfiguration | None:
        return db.scalar(
            select(ProviderConfiguration)
            .join(AIProvider)
            .where(AIProvider.key == key, ProviderConfiguration.is_default.is_(True))
        )

    # -- credentials ----------------------------------------------------------

    def credential(self, db: Session, key: str) -> ResolvedCredential:
        cls = self.provider_class(key)
        if cls.api_key_setting is None:
            return ResolvedCredential(source=CredentialSource.NONE)
        env_value = getattr(self.settings, cls.api_key_setting)
        if env_value is not None and env_value.get_secret_value().strip():
            secret = env_value.get_secret_value().strip()
            return ResolvedCredential(
                source=CredentialSource.ENVIRONMENT, api_key=secret, hint=secret_hint(secret)
            )
        config = self._default_config(db, key)
        if config is None or not config.api_key_encrypted:
            return ResolvedCredential(source=CredentialSource.NONE)
        try:
            secret = self.secrets.decrypt(config.api_key_encrypted)
        except SecretDecryptionError as exc:
            return ResolvedCredential(
                source=CredentialSource.STORED,
                hint=config.api_key_hint,
                config_id=config.id,
                error=str(exc),
            )
        return ResolvedCredential(
            source=CredentialSource.STORED,
            api_key=secret,
            hint=config.api_key_hint,
            config_id=config.id,
        )

    def unavailable_reason(self, key: str) -> str | None:
        return self.provider_class(key).unavailable_reason(self.settings)

    def base_url(self, db: Session, key: str) -> tuple[str | None, CredentialSource]:
        """The address of a server the user runs: environment first, then stored."""
        cls = self.provider_class(key)
        if cls.base_url_setting is None:
            return getattr(self.settings, f"{key}_base_url", None), CredentialSource.NONE
        env_value = getattr(self.settings, cls.base_url_setting, None)
        if env_value:
            return str(env_value), CredentialSource.ENVIRONMENT
        config = self._default_config(db, key)
        stored = (config.settings or {}).get("base_url") if config is not None else None
        if isinstance(stored, str) and stored:
            return stored, CredentialSource.STORED
        return None, CredentialSource.NONE

    def is_configured(self, db: Session, key: str) -> bool:
        cls = self.provider_class(key)
        if cls.unavailable_reason(self.settings) is not None:
            return False
        if cls.base_url_setting is not None and self.base_url(db, key)[0] is None:
            return False
        return cls.api_key_setting is None or self.credential(db, key).usable

    def store_base_url(self, db: Session, key: str, base_url: str) -> None:
        cls = self.provider_class(key)
        if cls.base_url_setting is None:
            raise InvalidInputError(f"{cls.display_name} has a fixed address.")
        try:
            url = check_server_url(base_url)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        config = self._default_config(db, key)
        if config is None:
            config = ProviderConfiguration(provider_id=self._provider_row(db, key).id)
            db.add(config)
        config.settings = {**(config.settings or {}), "base_url": url}
        db.commit()
        log.info("provider address stored", extra={"provider": key})

    def delete_base_url(self, db: Session, key: str) -> None:
        self.provider_class(key)
        config = self._default_config(db, key)
        if config is not None and (config.settings or {}).get("base_url"):
            config.settings = {k: v for k, v in config.settings.items() if k != "base_url"}
            db.commit()

    def store_key(self, db: Session, key: str, api_key: str) -> None:
        cls = self.provider_class(key)
        if cls.api_key_setting is None:
            raise InvalidInputError(f"{cls.display_name} does not use an API key.")
        api_key = api_key.strip()
        if not api_key or len(api_key) > MAX_KEY_LENGTH or any(ch.isspace() for ch in api_key):
            raise InvalidInputError("That does not look like an API key.")
        config = self._default_config(db, key)
        if config is None:
            config = ProviderConfiguration(provider_id=self._provider_row(db, key).id)
            db.add(config)
        config.api_key_encrypted = self.secrets.encrypt(api_key)
        config.api_key_hint = secret_hint(api_key)
        config.credential_source = CredentialSource.STORED
        db.commit()
        log.info("provider key stored", extra={"provider": key})

    def delete_key(self, db: Session, key: str) -> None:
        self.provider_class(key)
        config = self._default_config(db, key)
        if config is not None:
            config.api_key_encrypted = None
            config.api_key_hint = None
            config.credential_source = CredentialSource.NONE
            db.commit()
            log.info("provider key removed", extra={"provider": key})

    # -- adapters -------------------------------------------------------------

    def instantiate(self, db: Session, key: str) -> tuple[Provider, ResolvedCredential]:
        cls = self.provider_class(key)
        credential = self.credential(db, key)
        if cls.api_key_setting is not None and not credential.usable:
            raise ProviderNotConfiguredError(
                credential.error
                or f"{cls.display_name} is not configured. Add an API key in Settings.",
                details={"provider": key},
            )
        provider = cls(
            api_key=credential.api_key,
            base_url=self.base_url(db, key)[0],
            transport=self.transport,
            max_model_bytes=self.settings.max_model_bytes,
        )
        return provider, credential

    def check(self, db: Session, key: str) -> CredentialCheck:
        provider, _credential = self.instantiate(db, key)
        try:
            return provider.check_credentials()
        except ProviderError as exc:
            return CredentialCheck(ok=False, message=exc.message)
        finally:
            provider.close()
