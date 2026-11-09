"""Credential vault — transparent at-rest encryption for provider credentials.

When CONNEXA_ENC_KEY is set, the two credential stores (linked-account
credentials and per-user connector credentials) are sealed with a Fernet
envelope before touching the disk and opened transparently on read. Existing
plaintext rows keep working (unsealed values read through untouched), so the
key can be added to a live instance.

Why a TypeDecorator: sealing at the driver boundary means *every* code path —
sync engine, agent tool calls, OAuth exhange, console pages — is protected
without touching a single call site, and nobody can forget.
"""
from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.types import JSON, TypeDecorator

from .config import settings

PREFIX = "enc.v1:"


def _fernet() -> Fernet | None:
    if not settings.enc_key:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.enc_key.encode()).digest())
    return Fernet(key)


def seal(value: dict | None) -> dict | str | None:
    """dict -> sealed envelope string when the vault key is configured."""
    f = _fernet()
    if f is None or value is None or value == {}:
        return value
    return PREFIX + f.encrypt(json.dumps(value).encode()).decode()


def unseal(value: dict | str | None) -> dict | None:
    """envelope string (or legacy plaintext dict) -> dict. A lost key yields {}:
    the affected connection reports reauth_required rather than leaking."""
    if value is None or isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    f = _fernet()
    if f is None:
        return {}
    try:
        return json.loads(f.decrypt(value[len(PREFIX):].encode()).decode())
    except (InvalidToken, ValueError):
        return {}


class CredsJSON(TypeDecorator):
    """A JSON dict column that is sealed at rest when a vault key is configured."""

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return seal(value if value is not None else {})

    def process_result_value(self, value, dialect):
        return unseal(value) if value is not None else {}
