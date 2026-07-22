"""Credentials management for Google API access.

Supports two authentication modes:
1. ExtraSuite server - v2 session-token protocol
2. Service account file - direct credentials from JSON key file
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from slidesmith.engine.json_utils import read_json
from slidesmith.auth import browser_flow as _browser_flow
from slidesmith.auth.browser_flow import (
    GOG_BARE_TOKEN_REMEDIATION,  # noqa: F401
    SSL_CONTEXT,  # noqa: F401
    BareTokenProbeResult,  # noqa: F401
    BrowserFlowMixin,  # noqa: F401
    _exchange_refresh_token,  # noqa: F401
    _GOOGLE_TOKEN_URL,  # noqa: F401
    _OAUTH_USER_SCOPES,  # noqa: F401
    _post_form_json,  # noqa: F401
    probe_bare_token as _probe_bare_token,
)
from slidesmith.auth.discovery import (
    GogcliDiscoveryResult,  # noqa: F401
    OAuthClientCredentials,  # noqa: F401
    _find_gogcli_client_credentials,  # noqa: F401
    _find_gws_client_credentials,  # noqa: F401
    _inspect_gogcli_client_credentials,  # noqa: F401
    _parse_oauth_client_json,  # noqa: F401
)
from slidesmith.auth.doctor import auth_doctor_lines as _auth_doctor_lines
from slidesmith.auth.errors import AuthError, SessionExpiredError
from slidesmith.auth.stores import (
    FallbackSessionStore,  # noqa: F401
    FileSessionStore,  # noqa: F401
    InMemorySessionStore,  # noqa: F401
    KeyringSessionStore,  # noqa: F401
    SessionStore,  # noqa: F401
    SessionToken,  # noqa: F401
    _DEFAULT_PROFILE,  # noqa: F401
    _KEYRING_AVAILABLE,  # noqa: F401
    _KEYRING_SERVICE,  # noqa: F401
    _keyring,  # noqa: F401
    _write_secure_json,  # noqa: F401
)

# Backward-compatible module objects used by existing integrations and tests.
http = _browser_flow.http
secrets = _browser_flow.secrets
urllib = _browser_flow.urllib

# Scope changes require ``slidesmith auth login`` once to grant fresh consent.
# Existing stored sessions minted with the previous scopes continue to work.

_NO_AUTH_MESSAGE = """\
No authentication method found.

slidesmith checks for credentials in this order:

  1. ExtraSuite gateway
       EXTRASUITE_SERVER_URL env var
       ~/.config/extrasuite/gateway.json

  2. Service account file
       SERVICE_ACCOUNT_PATH env var

  3. gws (pre-obtained token)
       GOOGLE_WORKSPACE_CLI_TOKEN env var

  4. gws (OAuth client)
       GOOGLE_WORKSPACE_CLI_CLIENT_ID + GOOGLE_WORKSPACE_CLI_CLIENT_SECRET env vars
       GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE env var
       ~/.config/gws/client_secret.json

  5. gogcli (pre-obtained token)
       GOG_ACCESS_TOKEN env var

  6. gogcli (OAuth client)
       ~/.config/gogcli/credentials.json
       (~/Library/Application Support/gogcli/ on macOS)

Quick start options:
  Already use gws?    Run: gws auth setup   (then re-run your slidesmith command)
  Already use gogcli? Run: gog auth credentials <path/to/client_secret.json>
  Team deployment?    Run: slidesmith auth login   (requires gateway server)\
"""


@dataclass
class Credential:
    """A Google API access token."""

    token: str
    expires_at: float | None = None


def _unpack_oauth_result(
    result: tuple[str, str | None, float] | tuple[str, str]
) -> tuple[str, str | None, float]:
    """Normalize the browser-flow result while keeping older test seams valid."""
    if len(result) == 2:
        access_token, refresh_token = result
        return access_token, refresh_token, time.time() + 3600
    return result


def _report_access_only_session() -> None:
    """Explain the bounded session created when Google withholds a refresh token."""
    print(
        "Google granted access but withheld a refresh token. This session lasts "
        "about 1 hour. Revoke Slidesmith at "
        "https://myaccount.google.com/permissions and try again, or use your own "
        "OAuth client.",
        file=sys.stderr,
    )


def _parse_first_google_credential(
    response: dict[str, Any], cmd_type: str
) -> Credential:
    """Extract the first Google credential from a TokenResponse dict."""
    raw_creds: list[dict[str, Any]] = response.get("credentials", [])
    if not raw_creds:
        raise ValueError(
            f"Server returned no credentials for command type {cmd_type!r}"
        )

    # Pick the first Google credential (today there is always exactly one)
    raw = next(
        (c for c in raw_creds if c.get("provider", "google") == "google"), raw_creds[0]
    )

    expires_at: float | None = None
    expires_in = raw.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in >= 0:
        expires_at = time.time() + float(expires_in)
    return Credential(token=raw["token"], expires_at=expires_at)


def auth_doctor_lines() -> list[str]:
    """Return a layered, secret-safe authentication diagnosis."""
    return _auth_doctor_lines(
        find_gws_client_credentials=_find_gws_client_credentials,
        find_gogcli_client_credentials=_find_gogcli_client_credentials,
        inspect_gogcli_client_credentials=_inspect_gogcli_client_credentials,
        probe_bare_token=_probe_bare_token,
    )


class CredentialsManager(BrowserFlowMixin):
    """Manages credentials for Google API access.

    Supports two authentication modes:
    1. ExtraSuite protocol - obtains short-lived tokens via the v2 session flow
    2. Service account file - uses credentials from a JSON key file

    Refreshable sessions store their refresh token in the OS keyring (macOS
    Keychain, Linux SecretService, Windows Credential Locker) and a 0600
    Slidesmith file. If Google withholds a refresh token, the access-only
    session stores its short-lived access token instead, expiring in about one
    hour. OAuth client secrets are never written there.

    Precedence order for configuration:
    1. Constructor parameters
    2. Environment variables (EXTRASUITE_SERVER_URL)
    3. ~/.config/extrasuite/gateway.json (created by install script)
    4. service_account_path constructor parameter / SERVICE_ACCOUNT_PATH env var

    Args:
        server_url: Base URL for the ExtraSuite server
            (e.g., "https://server.com").
        service_account_path: Path to service account JSON file (optional).
        gateway_config_path: Path to gateway.json. Defaults to
            ~/.config/extrasuite/gateway.json.  If explicitly set and file
            doesn't exist, raises FileNotFoundError.
    """

    GATEWAY_CONFIG_PATH = Path.home() / ".config" / "extrasuite" / "gateway.json"
    DEFAULT_CALLBACK_TIMEOUT = 300  # 5 minutes for headless mode

    def __init__(
        self,
        server_url: str | None = None,
        service_account_path: str | Path | None = None,
        gateway_config_path: str | Path | None = None,
        headless: bool | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        # Store explicit gateway path (used by _load_gateway_config)
        self._gateway_config_path = (
            Path(gateway_config_path) if gateway_config_path else None
        )

        # Headless mode: no browser, print URL and prompt for code on stderr
        # Precedence: constructor param > EXTRASUITE_HEADLESS env var
        if headless is not None:
            self._headless = headless
        else:
            self._headless = os.environ.get("EXTRASUITE_HEADLESS", "").strip() == "1"

        # Resolve configuration with precedence: constructor > env var > gateway.json
        self._server_base_url = (
            server_url or os.environ.get("EXTRASUITE_SERVER_URL") or None
        )
        if self._server_base_url:
            self._server_base_url = self._server_base_url.rstrip("/")

        if not self._server_base_url:
            gateway_urls = self._load_gateway_config()
            if gateway_urls:
                self._server_base_url = gateway_urls.get("server_base_url")

        sa_path = service_account_path or os.environ.get("SERVICE_ACCOUNT_PATH")
        self._sa_path = Path(sa_path) if sa_path else None

        # Determine auth mode (checked in precedence order)
        self._bare_token: str | None = None
        self._oauth_client_creds: OAuthClientCredentials | None = None

        if self._server_base_url:
            self._auth_mode = "extrasuite"
        elif self._sa_path:
            self._auth_mode = "service_account"
        else:
            # Layer 3: bare access token from env var
            bare = os.environ.get("GOOGLE_WORKSPACE_CLI_TOKEN") or os.environ.get(
                "GOG_ACCESS_TOKEN"
            )
            if bare:
                self._auth_mode = "bare_token"
                self._bare_token = bare
            else:
                # Layer 4: gws OAuth client, then Layer 5: gogcli OAuth client
                oauth_creds = _find_gws_client_credentials()
                if oauth_creds is None:
                    oauth_creds = _find_gogcli_client_credentials()
                if oauth_creds is not None:
                    self._auth_mode = "oauth_client"
                    self._oauth_client_creds = oauth_creds
                else:
                    raise ValueError(_NO_AUTH_MESSAGE)

        self._session_store_injected = session_store is not None
        self._keyring_session_store: SessionStore | None = None
        self._file_session_store: SessionStore | None = None
        if session_store is not None:
            self._session_store: SessionStore = session_store
        elif self._auth_mode in ("extrasuite", "oauth_client"):
            self._keyring_session_store = KeyringSessionStore()
            self._file_session_store = FileSessionStore()
            store_choice = os.environ.get("SLIDESMITH_TOKEN_STORE", "").strip()
            if store_choice not in ("", "keyring", "file"):
                raise ValueError(
                    "SLIDESMITH_TOKEN_STORE must be 'keyring' or 'file', "
                    f"got {store_choice!r}"
                )
            if store_choice == "keyring":
                if not _KEYRING_AVAILABLE:
                    raise RuntimeError(
                        "SLIDESMITH_TOKEN_STORE=keyring but keyring is not available"
                    )
                self._session_store = self._keyring_session_store
            elif store_choice == "file":
                self._session_store = self._file_session_store
            else:
                self._session_store = FallbackSessionStore(
                    self._keyring_session_store, self._file_session_store
                )
        else:
            self._session_store = InMemorySessionStore()

        # Migrate legacy plain-text files from pre-keyring versions
        self._migrate_legacy_files()

    @property
    def auth_mode(self) -> str:
        """Return the selected authentication mode for CLI transport setup."""
        return self._auth_mode

    def probe_bare_token(self, token: str) -> BareTokenProbeResult:
        """Probe a bare token through the shared, fixed-host tokeninfo helper."""
        return _probe_bare_token(token)

    def get_credential(
        self,
        *,
        command: dict[str, Any],
        reason: str,
    ) -> Credential:
        """Exchange a session token for the credential(s) required by *command*.

        ``command`` must be a dict that matches one of the typed Command models
        on the server, e.g.::

            {"type": "sheet.pull", "file_url": "https://docs.google.com/..."}
            {"type": "gmail.compose", "subject": "Hello", "recipients": ["a@b.com"]}

        ``reason`` is agent-supplied user intent logged server-side for auditing.

        For ExtraSuite mode: validates the session, POSTs to /api/auth/token and
        returns the credential. The short-lived access token is not cached;
        only the ExtraSuite session token is stored.

        For service account file mode: generates a token directly from the SA key.
        Only meaningful for SA-backed command types; DWD is not supported in this
        mode.

        Args:
            command: Dict representation of a typed Command (must include ``type``).
            reason: Agent-supplied user intent (logged for auditing).
        Returns:
            A Credential object with the issued token.
        """
        cmd_type = command.get("type", "")

        if self._auth_mode == "extrasuite":
            return self._get_extrasuite_credential(
                command=command,
                cmd_type=cmd_type,
                reason=reason,
            )
        elif self._auth_mode == "service_account":
            return self._get_service_account_credential()
        elif self._auth_mode == "bare_token":
            assert self._bare_token is not None
            return Credential(token=self._bare_token)
        elif self._auth_mode == "oauth_client":
            return self._get_oauth_client_credential()
        else:
            raise RuntimeError(f"Unknown auth mode: {self._auth_mode!r}")

    def refresh_credential(
        self,
        *,
        command: dict[str, Any],
        reason: str,
    ) -> Credential | None:
        """Refresh an existing credential without opening a browser.

        Bare tokens and OAuth sessions that only contain an access token cannot
        be refreshed; callers should let the transport surface its recovery
        guidance instead.
        """
        if self._auth_mode in ("bare_token",):
            return None
        if self._auth_mode == "oauth_client":
            creds = self._oauth_client_creds
            assert creds is not None
            profile = f"{creds.source}-default"
            stored = self._load_session_token(profile)
            if stored is None or not stored.is_refreshable:
                return None
            if stored.raw_token is None:
                return None
            try:
                access_token, expires_at = _exchange_refresh_token(
                    creds.client_id, creds.client_secret, stored.raw_token
                )
            except Exception:
                return None
            # Re-save through the normal dual-store path so a recovered
            # Keychain/file projection remains current.
            self._save_session_token(stored, profile)
            return Credential(token=access_token, expires_at=expires_at)
        return self.get_credential(command=command, reason=reason)

    # =========================================================================
    # Session Token Methods
    # =========================================================================

    def _load_session_token(
        self, profile_name: str | None = None
    ) -> SessionToken | None:
        """Load a session token and best-effort mirror it across stores."""
        name = profile_name or _DEFAULT_PROFILE
        token = self._session_store.load(name)
        if token is not None:
            self._mirror_loaded_session_token(name, token)
        return token

    def _mirror_loaded_session_token(
        self, profile_name: str, token: SessionToken
    ) -> None:
        """Copy a loaded token to a missing or older persistent peer store."""
        if self._session_store_injected or self._auth_mode not in (
            "extrasuite",
            "oauth_client",
        ):
            return

        stores = (
            ("keyring", self._keyring_session_store),
            ("file", self._file_session_store),
        )
        for store_name, store in stores:
            if store is None:
                continue
            try:
                existing = store.load(profile_name)
            except Exception:
                existing = None
            if existing is not None and existing.expires_at >= token.expires_at:
                continue
            try:
                store.save(profile_name, token)
            except Exception as exc:
                print(
                    f"warning: could not mirror session token to {store_name} "
                    f"store ({exc!r})",
                    file=sys.stderr,
                )

    def _save_session_token(self, token: SessionToken, profile_name: str) -> None:
        """Save a session token to both persistent stores when possible."""
        if self._session_store_injected or self._auth_mode not in (
            "extrasuite",
            "oauth_client",
        ):
            self._session_store.save(profile_name, token)
        else:
            assert self._keyring_session_store is not None
            assert self._file_session_store is not None
            mirror = (
                self._session_store
                if isinstance(self._session_store, FallbackSessionStore)
                else FallbackSessionStore(
                    self._keyring_session_store, self._file_session_store
                )
            )
            mirror.save(profile_name, token)

    def _revoke_server_side(self, raw_token: str) -> None:
        """Revoke a session token on the server.  Best-effort; logs warning on failure."""
        if not self._server_base_url:
            return
        try:
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            revoke_url = f"{self._server_base_url}/api/admin/sessions/{token_hash}"
            req = urllib.request.Request(
                revoke_url,
                headers={"Authorization": f"Bearer {raw_token}"},
                method="DELETE",
            )
            try:
                urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT)
            except Exception as e:
                print(
                    f"warning: server-side session revocation failed ({e}).\n"
                    "Local credentials cleared, but your session may still be active on the server.",
                    file=sys.stderr,
                )
        except Exception:
            pass

    def _delete_session_token(self, profile_name: str) -> None:
        """Remove the session token for a profile from storage."""
        if self._session_store_injected or self._auth_mode not in (
            "extrasuite",
            "oauth_client",
        ):
            self._session_store.delete(profile_name)
            return
        for store in (self._keyring_session_store, self._file_session_store):
            if store is not None:
                with contextlib.suppress(Exception):
                    store.delete(profile_name)

    def _migrate_legacy_files(self) -> None:
        """Delete legacy plain-text session/credential files from pre-keyring versions."""
        legacy_session = Path.home() / ".config" / "extrasuite" / "session.json"
        legacy_creds_dir = Path.home() / ".config" / "extrasuite" / "credentials"
        with contextlib.suppress(Exception):
            legacy_session.unlink(missing_ok=True)
        if legacy_creds_dir.exists():
            for path in legacy_creds_dir.glob("*.json"):
                with contextlib.suppress(Exception):
                    path.unlink(missing_ok=True)
            with contextlib.suppress(Exception):
                legacy_creds_dir.rmdir()

    # =========================================================================
    # Public auth commands
    # =========================================================================

    def login(self, *, force: bool = False) -> SessionToken:
        """Log in and obtain a 30-day session token.

        If a valid session already exists and force=False, returns it immediately.

        If force=True, revokes any existing session server-side before issuing a
        new one.  This is the correct way to rotate credentials if a session may
        be compromised.

        Note: Device fingerprint information (MAC address, hostname, OS, and
        platform) is sent only in the ExtraSuite-server flow. Slidesmith's
        default ``oauth_client`` mode never collects or sends it.

        Args:
            force: If True, always create a new session even if one exists.
        Returns:
            A valid SessionToken.
        """
        if self._auth_mode == "oauth_client":
            assert self._oauth_client_creds is not None
            creds = self._oauth_client_creds
            profile_name = f"{creds.source}-default"
            if not force:
                existing = self._load_session_token(profile_name)
                if existing:
                    return existing
            self._delete_session_token(profile_name)
            access_token, refresh_token, expires_at = _unpack_oauth_result(
                self._run_oauth_browser_flow(
                    creds.client_id, creds.client_secret
                )
            )
            if not refresh_token:
                _report_access_only_session()
            session = SessionToken(
                raw_token=refresh_token,
                access_token=None if refresh_token else access_token,
                email="",
                expires_at=(
                    time.time() + 30 * 86400
                    if refresh_token
                    else expires_at
                ),
                is_refreshable=bool(refresh_token),
            )
            self._save_session_token(session, profile_name)
            return session

        if self._auth_mode != "extrasuite":
            raise RuntimeError(
                f"browser login is not available for auth mode {self._auth_mode!r}"
            )

        profile_name = _DEFAULT_PROFILE
        if force:
            existing = self._load_session_token(profile_name)
            if existing:
                self._revoke_server_side(existing.raw_token)
            self._delete_session_token(profile_name)
        session = self._get_or_create_session_token(
            force=force, profile_name=profile_name
        )
        return session

    # =========================================================================
    # Credential exchange (no disk caching)
    # =========================================================================

    def _get_extrasuite_credential(
        self,
        *,
        command: dict[str, Any],
        cmd_type: str,
        reason: str,
    ) -> Credential:
        """Get credential via ExtraSuite server (v2 session flow).

        Always fetches fresh — access tokens are never written to disk.
        """
        session = self._get_or_create_session_token()
        result = self._exchange_session_for_credential(
            session, command=command, reason=reason
        )
        return _parse_first_google_credential(result, cmd_type)

    def _get_service_account_credential(self) -> Credential:
        """Get credential from a service account JSON key file.

        Generates a fresh token on every call — no disk caching.
        """
        try:
            from google.auth.transport.requests import (  # type: ignore[import-not-found]
                Request,
            )
            from google.oauth2 import (  # type: ignore[import-not-found]
                service_account,
            )
        except ImportError:
            raise ImportError(  # noqa: B904
                "google-auth package is required for service account authentication. "
                "Install it with: pip install google-auth"
            )

        if not self._sa_path or not self._sa_path.exists():
            raise FileNotFoundError(f"Service account file not found: {self._sa_path}")

        credentials = service_account.Credentials.from_service_account_file(
            str(self._sa_path),
            scopes=[
                "https://www.googleapis.com/auth/presentations",
            ],
        )
        credentials.refresh(Request())

        expires_at = None
        expiry = getattr(credentials, "expiry", None)
        if expiry is not None:
            expires_at = expiry.timestamp()
        return Credential(token=credentials.token, expires_at=expires_at)

    def _get_oauth_client_credential(self) -> Credential:
        """Get a credential using a borrowed OAuth client from gws or gogcli.

        On first call: runs a browser flow and stores the refresh token in the
        OS keyring, or stores the short-lived access token as an access-only
        session when Google withholds a refresh token. On subsequent calls,
        refreshable sessions exchange the stored refresh token without browser
        interaction.
        """
        creds = self._oauth_client_creds
        assert (
            creds is not None
        )  # invariant: always set when _auth_mode == "oauth_client"
        profile = f"{creds.source}-default"

        stored = self._load_session_token(profile)
        if stored:
            if not stored.is_refreshable:
                if stored.access_token is None:
                    self._delete_session_token(profile)
                else:
                    return Credential(
                        token=stored.access_token, expires_at=stored.expires_at
                    )
            elif stored.raw_token is not None:
                try:
                    access_token, expires_at = _exchange_refresh_token(
                        creds.client_id, creds.client_secret, stored.raw_token
                    )
                    return Credential(token=access_token, expires_at=expires_at)
                except Exception:
                    # Refresh token revoked or expired — fall through to re-auth
                    self._delete_session_token(profile)

        access_token, refresh_token, expires_at = _unpack_oauth_result(
            self._run_oauth_browser_flow(creds.client_id, creds.client_secret)
        )
        if not refresh_token:
            _report_access_only_session()
        self._save_session_token(
            SessionToken(
                raw_token=refresh_token,
                access_token=None if refresh_token else access_token,
                email="",  # Google token response does not include an email address.
                expires_at=(
                    time.time() + 30 * 86400
                    if refresh_token
                    else expires_at
                ),
                is_refreshable=bool(refresh_token),
            ),
            profile,
        )
        return Credential(token=access_token, expires_at=expires_at)

    def _load_gateway_config(self) -> dict[str, str] | None:
        """Load endpoint URLs from gateway.json if it exists.

        Supports this format in gateway.json:
        - EXTRASUITE_SERVER_URL: Base URL for the server (preferred)

        Returns:
            Dictionary with the resolved server base URL, or None if file not found.

        Raises:
            FileNotFoundError: If explicit gateway_config_path was set and doesn't exist.
        """
        config_path = self._gateway_config_path or self.GATEWAY_CONFIG_PATH

        if self._gateway_config_path and not config_path.exists():
            raise FileNotFoundError(f"Gateway config file not found: {config_path}")

        if not config_path.exists():
            return None
        try:
            data = read_json(config_path, missing_ok=False)

            result: dict[str, str] = {}

            server_url = data.get("EXTRASUITE_SERVER_URL")
            if server_url:
                server_url = server_url.rstrip("/")
                result["server_base_url"] = server_url

            return result if result else None
        except (ValueError, OSError):
            return None

    # =========================================================================
    # Session Token Creation (v2 Protocol)
    # =========================================================================

    @staticmethod
    def _collect_device_info() -> dict[str, str]:
        """Collect device fingerprint for session token issuance."""
        return {
            "device_mac": hex(uuid.getnode()),
            "device_hostname": socket.gethostname(),
            "device_os": platform.system(),
            "device_platform": platform.platform(),
        }

    def _get_or_create_session_token(
        self, force: bool = False, profile_name: str | None = None
    ) -> SessionToken:
        """Get an existing valid session token or create a new one.

        If a valid session exists in the keyring, returns it immediately.
        Otherwise initiates Phase 1: browser/headless OAuth flow to get an auth
        code, then exchanges it for a 30-day session token.

        Args:
            force: If True, always create a new session (skips cache check).
            profile_name: Internal session-store key; defaults to ``default``.
        """
        name = profile_name or _DEFAULT_PROFILE
        if not force:
            cached = self._load_session_token(name)
            if cached:
                return cached

        # Fail fast before opening a browser: if server_base_url isn't set we cannot
        # complete the session exchange even if the user authenticates successfully.
        if self._server_base_url is None:
            raise RuntimeError(
                "server_base_url is not configured; cannot use session flow. "
                "Set EXTRASUITE_SERVER_URL or add it to gateway.json."
            )

        # Run browser/headless flow to get auth code
        auth_code, code_verifier = self._run_browser_flow_for_session()
        session_exchange_url = f"{self._server_base_url}/api/auth/session/exchange"
        device_info = self._collect_device_info()
        body = json.dumps(
            {"code": auth_code, "code_verifier": code_verifier, **device_info}
        ).encode("utf-8")

        req = urllib.request.Request(
            session_exchange_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                req, timeout=30, context=SSL_CONTEXT
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 400:
                raise AuthError(
                    "Auth code invalid or expired. Please re-authenticate: "
                    "slidesmith auth login"
                ) from e
            error_body = e.read().decode("utf-8") if e.fp else str(e)
            raise AuthError(f"Session token exchange failed: {error_body}") from e
        except urllib.error.URLError as e:
            raise AuthError(f"Failed to connect to server: {e}") from e

        expires_at_dt = datetime.fromisoformat(
            result["expires_at"].replace("Z", "+00:00")
        )
        session = SessionToken(
            raw_token=result["session_token"],
            email=result["email"],
            expires_at=expires_at_dt.timestamp(),
        )
        self._save_session_token(session, name)
        return session

    def _exchange_session_for_credential(
        self,
        session: SessionToken,
        *,
        command: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """Exchange a session token for credential(s) via a typed Command (Phase 2).

        Returns the raw response dict:
        ``{"credentials": [...], "command_type": "..."}``
        """
        if self._server_base_url is None:
            raise RuntimeError(
                "server_base_url is not configured; cannot use session flow"
            )
        access_token_url = f"{self._server_base_url}/api/auth/token"
        body = json.dumps({"command": command, "reason": reason}).encode("utf-8")

        req = urllib.request.Request(
            access_token_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                # Session token goes in Authorization header (not body) to avoid
                # it being recorded in server/proxy access logs.
                "Authorization": f"Bearer {session.raw_token}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                req, timeout=30, context=SSL_CONTEXT
            ) as response:
                return json.loads(response.read().decode("utf-8"))  # type: ignore[return-value]
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else str(e)
            if e.code == 401:
                raise SessionExpiredError(
                    "Session expired or revoked. Run: slidesmith auth login"
                ) from e
            raise AuthError(f"Access token exchange failed: {error_body}") from e
        except urllib.error.URLError as e:
            raise AuthError(f"Failed to connect to server: {e}") from e
