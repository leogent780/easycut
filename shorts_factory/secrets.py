"""Local secrets storage: OS-native keyring by default, encrypted-file fallback for headless Linux.

Only ever stores a reference key in the SQLite state DB (see state.Credential.keyring_key_ref) —
the actual secret value (refresh token, API key) lives here, never in the DB.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

SERVICE_NAME = "shorts-factory"


class SecretsBackend:
    def get(self, key: str) -> str | None:
        raise NotImplementedError

    def set(self, key: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class KeyringBackend(SecretsBackend):
    """OS-native secure storage: macOS Keychain, Windows Credential Manager, Linux Secret Service."""

    def __init__(self) -> None:
        import keyring  # deferred import: only required when this backend is actually selected

        self._keyring = keyring

    def get(self, key: str) -> str | None:
        return self._keyring.get_password(SERVICE_NAME, key)

    def set(self, key: str, value: str) -> None:
        self._keyring.set_password(SERVICE_NAME, key, value)

    def delete(self, key: str) -> None:
        try:
            self._keyring.delete_password(SERVICE_NAME, key)
        except Exception:
            pass  # already absent — deleting a missing secret is not an error for our callers


class EncryptedFileBackend(SecretsBackend):
    """Fallback for headless Linux boxes with no Secret Service/keyring running.

    Encrypts with a Fernet key derived from SECRETS_FILE_PASSPHRASE (env var).
    Explicitly NOT the default — documented in .env.example as an opt-in fallback.
    """

    def __init__(self, path: str | Path | None = None, passphrase: str | None = None) -> None:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        passphrase = passphrase or os.environ.get("SECRETS_FILE_PASSPHRASE")
        if not passphrase:
            raise RuntimeError(
                "SECRETS_BACKEND=file requires SECRETS_FILE_PASSPHRASE to be set (see .env.example)"
            )

        self._path = Path(path or os.environ.get("SHORTS_FACTORY_DATA_DIR", "./data")) / "secrets.enc"
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Fixed salt is acceptable here: this is a single-user local file, not a multi-tenant store —
        # the passphrase itself (never committed, env-var only) is the actual secret.
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b"shorts-factory-static-salt", iterations=390_000)
        key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))
        self._fernet = Fernet(key)

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        import json

        decrypted = self._fernet.decrypt(self._path.read_bytes())
        return json.loads(decrypted.decode())

    def _save(self, data: dict) -> None:
        import json

        encrypted = self._fernet.encrypt(json.dumps(data).encode())
        self._path.write_bytes(encrypted)
        self._path.chmod(0o600)

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def set(self, key: str, value: str) -> None:
        data = self._load()
        data[key] = value
        self._save(data)

    def delete(self, key: str) -> None:
        data = self._load()
        data.pop(key, None)
        self._save(data)


_backend_instance: SecretsBackend | None = None


def get_backend() -> SecretsBackend:
    global _backend_instance
    if _backend_instance is not None:
        return _backend_instance

    backend_name = os.environ.get("SECRETS_BACKEND", "keyring")
    if backend_name == "file":
        _backend_instance = EncryptedFileBackend()
    else:
        _backend_instance = KeyringBackend()
    return _backend_instance


def get_secret(key: str) -> str | None:
    return get_backend().get(key)


def set_secret(key: str, value: str) -> None:
    get_backend().set(key, value)


def delete_secret(key: str) -> None:
    get_backend().delete(key)


def youtube_refresh_token_key(channel_name: str) -> str:
    return f"youtube_refresh_token:{channel_name}"
