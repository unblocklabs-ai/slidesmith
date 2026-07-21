"""Vulture use references for deliberate public and framework-owned surfaces."""

# Compatibility geometry helper directly exercised by the untouched donor suite.
_.absolute_from

# Retained public/compatibility helpers and diagnostic construction state.
parse_position_classes
_.get_google_id

# BaseHTTPRequestHandler owns this callback and its signature.
_.log_message
format_string

# http.client invokes these overrides and owns the socket attribute dynamically.
_.connect
_.sock

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
