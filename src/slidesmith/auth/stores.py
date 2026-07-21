"""Persistent stores for long-lived authentication session tokens."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from slidesmith.engine.json_utils import read_json

try:
    import keyring as _keyring

    _KEYRING_AVAILABLE = True
except ImportError:
    _keyring = None  # type: ignore[assignment]
    _KEYRING_AVAILABLE = False

_KEYRING_SERVICE = "extrasuite"
_DEFAULT_PROFILE = "default"


def _write_secure_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically replace a JSON file with mode 0600 from creation onward."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(stat.S_IRWXU)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd = os.open(
        temp_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        with handle:
            handle.write(content)
        os.replace(temp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise


@dataclass
class SessionToken:
    """Long-lived (30-day) session token for headless agent access."""

    raw_token: str
    email: str
    expires_at: float

    def is_valid(self, buffer_seconds: int = 300) -> bool:
        """Check if session token is still valid with a 5-minute buffer."""
        return time.time() < self.expires_at - buffer_seconds

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "raw_token": self.raw_token,
            "email": self.email,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionToken:
        """Create SessionToken from dictionary."""
        return cls(
            raw_token=data["raw_token"],
            email=data["email"],
            expires_at=data["expires_at"],
        )


class SessionStore(Protocol):
    """Protocol for session token storage backends."""

    def load(self, profile_name: str) -> SessionToken | None: ...
    def save(self, profile_name: str, token: SessionToken) -> None: ...
    def delete(self, profile_name: str) -> None: ...


class KeyringSessionStore:
    """Session token storage backed by the OS keyring."""

    @staticmethod
    def _backend() -> Any:
        # Preserve the historical monkeypatch surface on slidesmith.credentials.
        compat = sys.modules.get("slidesmith.credentials")
        available = getattr(compat, "_KEYRING_AVAILABLE", _KEYRING_AVAILABLE)
        backend = getattr(compat, "_keyring", _keyring)
        if not available or backend is None:
            raise RuntimeError("keyring package is not available")
        return backend

    def load(self, profile_name: str) -> SessionToken | None:
        raw = self._backend().get_password(_KEYRING_SERVICE, profile_name)
        if not raw:
            return None
        try:
            token = SessionToken.from_dict(json.loads(raw))
            return token if token.is_valid() else None
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            # Malformed-but-valid JSON (e.g. a list, or a non-numeric
            # expires_at) is treated as "no session", matching FileSessionStore.
            return None

    def save(self, profile_name: str, token: SessionToken) -> None:
        self._backend().set_password(
            _KEYRING_SERVICE, profile_name, json.dumps(token.to_dict())
        )

    def delete(self, profile_name: str) -> None:
        self._backend().delete_password(_KEYRING_SERVICE, profile_name)


class FileSessionStore:
    """Session token storage at ``~/.config/slidesmith/session.json``."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = (
            Path(path)
            if path is not None
            else Path.home() / ".config" / "slidesmith" / "session.json"
        )

    def _load_profiles(self) -> dict[str, dict[str, Any]]:
        try:
            data = read_json(self.path, missing_ok=True)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        profiles = data.get("profiles")
        if isinstance(profiles, dict):
            return {
                str(name): payload
                for name, payload in profiles.items()
                if isinstance(payload, dict)
            }
        if {"raw_token", "email", "expires_at"}.issubset(data):
            return {_DEFAULT_PROFILE: data}
        return {}

    def load(self, profile_name: str) -> SessionToken | None:
        payload = self._load_profiles().get(profile_name)
        if payload is None:
            return None
        try:
            token = SessionToken.from_dict(payload)
        except (KeyError, TypeError, ValueError):
            return None
        return token if token.is_valid() else None

    def save(self, profile_name: str, token: SessionToken) -> None:
        profiles = self._load_profiles()
        profiles[profile_name] = token.to_dict()
        _write_secure_json(self.path, {"profiles": profiles})

    def delete(self, profile_name: str) -> None:
        profiles = self._load_profiles()
        if profile_name not in profiles:
            return
        profiles.pop(profile_name)
        if profiles:
            _write_secure_json(self.path, {"profiles": profiles})
        else:
            self.path.unlink(missing_ok=True)


class FallbackSessionStore:
    """Read keyring first and fall back to a file after any keyring error."""

    def __init__(
        self,
        keyring_store: SessionStore | None = None,
        file_store: SessionStore | None = None,
    ) -> None:
        self.keyring_store = keyring_store or KeyringSessionStore()
        self.file_store = file_store or FileSessionStore()
        self._notice_printed = False

    def _notice(self, exc: Exception) -> None:
        if self._notice_printed:
            return
        self._notice_printed = True
        print(
            f"warning: keyring unavailable ({exc!r}); using file session store",
            file=sys.stderr,
        )

    def load(self, profile_name: str) -> SessionToken | None:
        try:
            token = self.keyring_store.load(profile_name)
        except Exception as exc:
            self._notice(exc)
            return self.file_store.load(profile_name)
        if token is not None:
            return token
        return self.file_store.load(profile_name)

    def save(self, profile_name: str, token: SessionToken) -> None:
        keyring_saved = False
        try:
            self.keyring_store.save(profile_name, token)
            keyring_saved = True
        except Exception as exc:
            self._notice(exc)
        try:
            self.file_store.save(profile_name, token)
        except Exception as exc:
            if not keyring_saved:
                raise
            print(
                f"warning: file session store unavailable ({exc!r}); "
                "session saved to keyring only",
                file=sys.stderr,
            )

    def delete(self, profile_name: str) -> None:
        try:
            self.keyring_store.delete(profile_name)
        except Exception as exc:
            self._notice(exc)
        self.file_store.delete(profile_name)


class InMemorySessionStore:
    """In-memory session token storage (for testing and non-persistent use)."""

    def __init__(self) -> None:
        self._tokens: dict[str, SessionToken] = {}

    def load(self, profile_name: str) -> SessionToken | None:
        token = self._tokens.get(profile_name)
        if token and token.is_valid():
            return token
        return None

    def save(self, profile_name: str, token: SessionToken) -> None:
        self._tokens[profile_name] = token

    def delete(self, profile_name: str) -> None:
        self._tokens.pop(profile_name, None)
