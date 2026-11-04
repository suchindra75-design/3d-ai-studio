import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ai3d.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from ai3d.models.enums import CredentialSource, ProviderKind


class AIProvider(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A provider known to this installation (implementations live in code)."""

    __tablename__ = "ai_providers"

    key: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[ProviderKind] = mapped_column(str_enum(ProviderKind))
    enabled: Mapped[bool] = mapped_column(default=False)
    # Snapshot of the provider's discovered capabilities.
    capabilities: Mapped[dict[str, Any]] = mapped_column(default=dict)
    description: Mapped[str] = mapped_column(Text, default="")
    homepage_url: Mapped[str | None] = mapped_column(String(512))

    configurations: Mapped[list["ProviderConfiguration"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan", passive_deletes=True
    )


class ProviderConfiguration(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """User configuration for a provider. Secrets are never stored in plain text."""

    __tablename__ = "provider_configurations"

    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_providers.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128), default="default")
    is_default: Mapped[bool] = mapped_column(default=True)
    # Non-secret settings (base URL override, model version, timeouts…).
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    credential_source: Mapped[CredentialSource] = mapped_column(
        str_enum(CredentialSource), default=CredentialSource.NONE
    )
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    # Last characters only, for display ("…a1b2").
    api_key_hint: Mapped[str | None] = mapped_column(String(16))

    provider: Mapped[AIProvider] = relationship(back_populates="configurations")

    __table_args__ = (UniqueConstraint("provider_id", "name"),)
