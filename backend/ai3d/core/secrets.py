"""Encryption of secrets stored in the database (provider API keys).

Secrets are encrypted with Fernet (AES-128-CBC + HMAC-SHA256). The key comes
from ``AI3D_SECRET_KEY``; without it a random key is generated once and kept in
``<data_dir>/secret.key`` with owner-only permissions. Stored secrets are then
exactly as safe as that file, which is why the environment variable is the
recommended setup for shared machines and servers.
"""

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ai3d.core.config import Settings

KEY_FILE_NAME = "secret.key"
MIN_SECRET_LENGTH = 16


class SecretDecryptionError(Exception):
    """A stored secret cannot be decrypted (e.g. the secret key changed)."""


def _fernet_key(material: str) -> bytes:
    raw = material.strip().encode()
    try:
        if len(base64.urlsafe_b64decode(raw)) == 32:
            return raw
    except (ValueError, TypeError):
        pass
    # Any other passphrase is stretched into a valid key. It should still be long and random.
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())


def _load_or_create_key_file(path: Path) -> bytes:
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    return key


class SecretBox:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def from_settings(cls, settings: Settings) -> "SecretBox":
        material = settings.secret_key.get_secret_value().strip() if settings.secret_key else ""
        if material:
            if len(material) < MIN_SECRET_LENGTH:
                raise ValueError(
                    f"AI3D_SECRET_KEY must be at least {MIN_SECRET_LENGTH} characters long."
                )
            return cls(_fernet_key(material))
        # Unset or empty (e.g. an empty variable from docker compose): use the key file.
        return cls(_load_or_create_key_file(settings.data_dir / KEY_FILE_NAME))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise SecretDecryptionError(
                "Stored secret cannot be decrypted; was AI3D_SECRET_KEY changed?"
            ) from exc


def secret_hint(secret: str) -> str:
    """Last four characters for display; never more."""
    return f"…{secret[-4:]}" if len(secret) >= 12 else "…"
