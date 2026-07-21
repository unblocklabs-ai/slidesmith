"""OAuth browser flows, loopback callback handling, PKCE, and state checks."""

from __future__ import annotations

import hashlib
import html
import http.server
import json
import math
import os
import secrets
import select
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Literal

from slidesmith.auth.errors import AuthError

try:
    import certifi

    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
_OAUTH_USER_SCOPES = [
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/drive.file",
    "openid",
    "email",
]

GOG_BARE_TOKEN_REMEDIATION = (
    "Your GOG_ACCESS_TOKEN is expired or invalid. gog sometimes exports a stale "
    "token — run a throwaway `gog` API request to force a refresh, then re-export "
    "GOG_ACCESS_TOKEN and retry."
)

BareTokenProbeResult = tuple[Literal["valid", "invalid", "unreachable"], float | None]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject tokeninfo redirects instead of forwarding the access token."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        _msg: str,
        headers: Any,
        _newurl: str,
    ) -> None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "Google tokeninfo redirects are disabled",
            headers,
            fp,
        )


def _post_form_json(url: str, fields: dict[str, str]) -> dict[str, Any]:
    """POST URL-encoded form fields and decode one JSON object response."""
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as response:
        result: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        return result


def _get_tokeninfo_json(access_token: str) -> dict[str, Any]:
    """POST tokeninfo to the fixed, HTTPS-verified Google endpoint."""
    parsed = urllib.parse.urlparse(_GOOGLE_TOKENINFO_URL)
    if parsed.scheme != "https" or parsed.hostname != "oauth2.googleapis.com":
        raise RuntimeError("Google tokeninfo endpoint must remain host-pinned")
    request = urllib.request.Request(
        _GOOGLE_TOKENINFO_URL,
        data=urllib.parse.urlencode({"access_token": access_token}).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=SSL_CONTEXT),
        _NoRedirectHandler(),
    )
    with opener.open(request, timeout=30) as response:
        status = response.getcode()
        if status is not None and 300 <= status < 400:
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                "Google tokeninfo redirects are disabled",
                response.headers,
                response,
            )
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Google tokeninfo response was not a JSON object")
    return result


def _numeric(value: Any) -> float | None:
    """Parse finite numeric tokeninfo fields, including Google's strings."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def probe_bare_token(access_token: str) -> BareTokenProbeResult:
    """Classify a bare token without making network failures fatal."""
    try:
        result = _get_tokeninfo_json(access_token)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401):
            return "invalid", None
        return "unreachable", None
    except (OSError, TimeoutError, ValueError):
        return "unreachable", None

    if result.get("error") or result.get("error_description"):
        return "invalid", None

    now = time.time()
    expires_in = _numeric(result.get("expires_in"))
    expires_at = now + expires_in if expires_in is not None else None
    exp = _numeric(result.get("exp"))
    if exp is not None:
        expires_at = exp
    if expires_at is not None and expires_at <= now:
        return "invalid", expires_at
    return "valid", expires_at


def _exchange_refresh_token(
    client_id: str, client_secret: str, refresh_token: str
) -> tuple[str, float]:
    """Exchange a refresh token for a new Google access token."""
    result = _post_form_json(
        _GOOGLE_TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    expires_at = time.time() + int(result.get("expires_in", 3600))
    return result["access_token"], expires_at


class BrowserFlowMixin:
    """Browser-based authentication behavior shared by the credential manager."""

    DEFAULT_CALLBACK_TIMEOUT: int
    _headless: bool
    _server_base_url: str | None

    def _run_oauth_browser_flow(
        self, client_id: str, client_secret: str
    ) -> tuple[str, str | None, float]:
        """Run an OAuth 2.0 authorization code flow with PKCE against Google."""
        import base64

        code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        digest = hashlib.sha256(code_verifier.encode()).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

        def auth_url_for_port(port: int, state: str) -> str:
            redirect_uri = f"http://127.0.0.1:{port}"
            params = urllib.parse.urlencode(
                {
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": " ".join(_OAUTH_USER_SCOPES),
                    "state": state,
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                    "access_type": "offline",
                    "prompt": "consent",
                }
            )
            return f"https://accounts.google.com/o/oauth2/v2/auth?{params}"

        code, port = self._run_browser_flow(
            auth_url_for_port, "Sign in with Google:"
        )
        redirect_uri = f"http://127.0.0.1:{port}"

        try:
            result = _post_form_json(
                _GOOGLE_TOKEN_URL,
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "code_verifier": code_verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
            )
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else str(e)
            raise AuthError(f"Google token exchange failed: {error_body}") from e

        expires_at = time.time() + int(result.get("expires_in", 3600))
        refresh_token = result.get("refresh_token")
        return result["access_token"], refresh_token, expires_at

    def _run_browser_flow(
        self,
        auth_url_for_port: Callable[[int, str], str],
        display_msg: str,
    ) -> tuple[str, int]:
        """Run browser OAuth and return the auth code plus bound callback port."""
        state = secrets.token_urlsafe(32)
        result_holder: dict[str, Any] = {"code": None, "error": None, "done": False}
        result_lock = threading.Lock()

        handler_class = self._create_handler_class(
            result_holder, result_lock, expected_state=state
        )
        server = http.server.HTTPServer(("127.0.0.1", 0), handler_class)
        port = int(server.server_port)
        auth_url = auth_url_for_port(port, state)
        server.timeout = 1

        def serve_loop() -> None:
            start_time = time.time()
            while time.time() - start_time < self.DEFAULT_CALLBACK_TIMEOUT:
                with result_lock:
                    if result_holder["done"]:
                        break
                server.handle_request()
            server.server_close()

        server_thread = threading.Thread(target=serve_loop, daemon=True)
        server_thread.start()

        print(f"{display_msg}\n\n  {auth_url}\n")
        try:
            import webbrowser

            webbrowser.open(auth_url)
        except Exception:
            pass
        print("Waiting for authentication...")

        def read_stdin() -> None:
            try:
                if not sys.stdin.isatty():
                    return
                while True:
                    with result_lock:
                        if result_holder["done"]:
                            return
                    if sys.platform != "win32":
                        ready, _, _ = select.select([sys.stdin], [], [], 1.0)
                        if not ready:
                            continue
                    line = sys.stdin.readline().strip()
                    if line:
                        with result_lock:
                            if not result_holder["done"]:
                                result_holder["code"] = line
                                result_holder["done"] = True
                        return
            except Exception:
                pass

        stdin_thread = threading.Thread(target=read_stdin, daemon=True)
        stdin_thread.start()

        start_time = time.time()
        while time.time() - start_time < self.DEFAULT_CALLBACK_TIMEOUT:
            with result_lock:
                if result_holder["done"]:
                    break
            time.sleep(0.5)

        with result_lock:
            result_holder["done"] = True
            error = result_holder.get("error")
            code = result_holder.get("code")

        if error:
            raise AuthError(f"Authentication failed: {error}")
        if not code:
            raise AuthError("Authentication timed out. Please try again.")
        return str(code), port

    def _run_browser_flow_for_session(self) -> tuple[str, str]:
        """Run OAuth browser flow and return the auth code and PKCE verifier."""
        import base64

        code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        digest = hashlib.sha256(code_verifier.encode()).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

        if self._headless:
            params = urllib.parse.urlencode(
                {
                    "state": secrets.token_urlsafe(32),
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                }
            )
            auth_url = f"{self._server_base_url}/api/token/auth?{params}"
            print(
                f"\nOpen this URL to authenticate:\n\n  {auth_url}\n",
                file=sys.stderr,
            )
            print(
                "After authenticating, copy the code shown on the page and paste it here: ",
                end="",
                flush=True,
                file=sys.stderr,
            )
            code_holder: list[str] = []

            def _read_code() -> None:
                try:
                    line = sys.stdin.readline().strip()
                    if line:
                        code_holder.append(line)
                except Exception:
                    pass

            reader = threading.Thread(target=_read_code, daemon=True)
            reader.start()
            reader.join(timeout=self.DEFAULT_CALLBACK_TIMEOUT)

            if not code_holder:
                raise AuthError(
                    f"No auth code provided within {self.DEFAULT_CALLBACK_TIMEOUT}s. Please try again."
                )
            return code_holder[0], code_verifier

        code, _ = self._run_browser_flow(
            lambda port, state: (
                f"{self._server_base_url}/api/token/auth?"
                + urllib.parse.urlencode(
                    {
                        "port": port,
                        "state": state,
                        "code_challenge": code_challenge,
                        "code_challenge_method": "S256",
                    }
                )
            ),
            "Open this URL to authenticate:",
        )
        return code, code_verifier

    @staticmethod
    def _create_handler_class(
        result_holder: dict[str, Any],
        result_lock: threading.Lock,
        expected_state: str,
    ) -> type:
        """Create HTTP handler class for OAuth callback."""

        class CallbackHandler(http.server.BaseHTTPRequestHandler):
            """HTTP handler to receive OAuth callback."""

            def log_message(self, format_string: str, *args: Any) -> None:
                """Suppress default logging."""
                pass

            def do_GET(self) -> None:
                """Handle GET request with auth code or error."""
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)

                with result_lock:
                    if result_holder["done"]:
                        self._send_html("Already processed.", 400)
                        return

                    callback_state = params.get("state", [""])[0]
                    if not secrets.compare_digest(callback_state, expected_state):
                        result_holder["error"] = (
                            "OAuth state mismatch. Please restart authentication."
                        )
                        result_holder["done"] = True
                        self._send_html(
                            """
                            <html>
                            <head><title>Authentication Failed</title></head>
                            <body style="font-family: sans-serif; padding: 40px; text-align: center;">
                                <h1 style="color: #dc3545;">Authentication Failed</h1>
                                <p>OAuth state mismatch. Please restart authentication.</p>
                            </body>
                            </html>
                            """,
                            400,
                        )
                    elif "error" in params:
                        result_holder["error"] = params["error"][0]
                        result_holder["done"] = True
                        escaped_error = html.escape(params["error"][0])
                        self._send_html(
                            f"""
                            <html>
                            <head><title>Authentication Failed</title></head>
                            <body style="font-family: sans-serif; padding: 40px; text-align: center;">
                                <h1 style="color: #dc3545;">Authentication Failed</h1>
                                <p>{escaped_error}</p>
                                <p>Please close this window and try again.</p>
                            </body>
                            </html>
                            """,
                            400,
                        )
                    elif "code" in params:
                        result_holder["code"] = params["code"][0]
                        result_holder["done"] = True
                        self._send_html(
                            """
                            <html>
                            <head><title>Authentication Successful</title></head>
                            <body style="font-family: sans-serif; padding: 40px; text-align: center;">
                                <h1 style="color: #28a745;">Authentication Successful!</h1>
                                <p>You can close this window and return to your terminal.</p>
                                <script>window.close();</script>
                            </body>
                            </html>
                            """
                        )
                    else:
                        self._send_html(
                            """
                            <html>
                            <head><title>Invalid Request</title></head>
                            <body style="font-family: sans-serif; padding: 40px; text-align: center;">
                                <h1>Invalid Request</h1>
                                <p>Missing auth code in callback.</p>
                            </body>
                            </html>
                            """,
                            400,
                        )

            def _send_html(self, content: str, status: int = 200) -> None:
                """Send HTML response."""
                self.send_response(status)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(content.encode())

        return CallbackHandler
