"""Vulture use references for deliberate public and framework-owned surfaces."""

# Compatibility geometry helper directly exercised by the untouched donor suite.
_.absolute_from

# Complete public enum/model surface; not every value is needed internally today.
RENDERED
ThemeColorType
DARK1
LIGHT1
DARK2
LIGHT2
ACCENT1
ACCENT2
ACCENT3
ACCENT4
ACCENT5
ACCENT6
TEXT1
TEXT2
BACKGROUND1
BACKGROUND2
HYPERLINK
FOLLOWED_HYPERLINK
provider
scopes

# Retained public/compatibility helpers and diagnostic construction state.
parse_position_classes
_.get_google_id
_._access_token
_._timeout

# BaseHTTPRequestHandler owns this callback and its signature.
_.log_message
format_string

# Credentials re-exports are the supported monkeypatch surface for tests/integrations.
SSL_CONTEXT
BrowserFlowMixin
_exchange_refresh_token
_GOOGLE_TOKEN_URL
_OAUTH_USER_SCOPES
_post_form_json
OAuthClientCredentials
_find_gogcli_client_credentials
_find_gws_client_credentials
_parse_oauth_client_json
FallbackSessionStore
FileSessionStore
InMemorySessionStore
KeyringSessionStore
SessionStore
SessionToken
_DEFAULT_PROFILE
_KEYRING_AVAILABLE
_KEYRING_SERVICE
_keyring
_write_secure_json

# Test doubles accept arbitrary constructor/transport keyword arguments by contract.
kwargs
