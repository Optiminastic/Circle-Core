"""Vapi authenticates to the bridge with a bearer token (VOICE_BRIDGE_SECRET)."""

from __future__ import annotations

import secrets

_BEARER_PREFIX = "Bearer "


def is_authorized(authorization: str | None, expected_secret: str) -> bool:
    """Constant-time check of an `Authorization: Bearer <secret>` header."""
    if not expected_secret or not authorization or not authorization.startswith(_BEARER_PREFIX):
        return False
    return secrets.compare_digest(authorization[len(_BEARER_PREFIX) :], expected_secret)
