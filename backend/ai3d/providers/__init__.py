"""3D generation providers. See ``base.Provider`` and docs/AI_PIPELINE.md §2."""

from ai3d.core.config import Settings
from ai3d.providers.base import Provider
from ai3d.providers.local_ai import LocalAIProvider
from ai3d.providers.local_photogrammetry import LocalPhotogrammetryProvider
from ai3d.providers.meshy import MeshyProvider
from ai3d.providers.tripo import TripoProvider

# Order = preference when the user lets the app choose.
_REAL_PROVIDERS: tuple[type[Provider], ...] = (
    TripoProvider,
    MeshyProvider,
    LocalPhotogrammetryProvider,
    LocalAIProvider,
)


class ProviderRegistry:
    """The provider implementations available in this installation."""

    def __init__(self, settings: Settings) -> None:
        classes: list[type[Provider]] = list(_REAL_PROVIDERS)
        if settings.enable_mock_provider:
            from ai3d.providers.mock import MockProvider  # noqa: PLC0415 - test-only

            classes.append(MockProvider)
        self._classes = {cls.key: cls for cls in classes}

    def get(self, key: str) -> type[Provider] | None:
        return self._classes.get(key)

    def all(self) -> list[type[Provider]]:
        return list(self._classes.values())


__all__ = ["Provider", "ProviderRegistry"]
