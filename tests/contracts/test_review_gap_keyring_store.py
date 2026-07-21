"""Regression coverage for malformed keyring session payloads."""

from __future__ import annotations

import json
from typing import Any

import pytest

from slidesmith import credentials
from slidesmith.auth.stores import KeyringSessionStore


class _PayloadKeyring:
    def __init__(self, payload: Any) -> None:
        self.payload = json.dumps(payload)

    def get_password(self, _service: str, _profile: str) -> str:
        return self.payload


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "a", "token", "object"],
        {
            "raw_token": "token",
            "email": "agent@example.com",
            "expires_at": "not-a-number",
        },
    ],
    ids=["json-list", "nonnumeric-expires-at"],
)
def test_keyring_malformed_valid_json_is_treated_as_missing(
    monkeypatch: pytest.MonkeyPatch,
    payload: Any,
) -> None:
    monkeypatch.setattr(credentials, "_KEYRING_AVAILABLE", True)
    monkeypatch.setattr(credentials, "_keyring", _PayloadKeyring(payload))

    assert KeyringSessionStore().load("default") is None
