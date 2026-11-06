"""OAuth 2.0 machinery for the agent lane — signed state, authorize URLs,
callback exchange and per-connected-user credential storage.

The flow mirrors the "social sign-in" pattern of commercial platforms:
agents render `authenticate_<slug>`, get a real authorization URL, a human
clicks it, the provider redirects to /oauth/<slug>/callback, and the platform
stores the token against the registered user for every later tool call.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from .config import settings
from .utils import uid


def _secret() -> bytes:
    return hashlib.sha256(f"connexa-oauth:{settings.admin_token}".encode()).digest()


def sign_state(payload: dict) -> str:
    """Stateless signed state — binds org/user/connector and expires."""
    body = base64.urlsafe_b64encode(json.dumps({**payload, "exp": int(time.time()) + 900}).encode())
    tag = hmac.new(_secret(), body, hashlib.sha256).hexdigest()[:32]
    return f"{body.decode()}.{tag}"


def parse_state(state: str) -> dict | None:
    if not state or "." not in state:
        return None
    body, _, tag = state.partition(".")
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, tag):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode()))
    except Exception:  # noqa: BLE001
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def build_authorize_url(connector, *, base_url: str, agent_org_id: str, registered_user_id: str) -> str:
    """Platform- and provider-shaped authorize URL bound to this user."""
    state = sign_state({"org": agent_org_id, "ru": registered_user_id, "slug": connector.slug})
    return connector.authorize_url(state=state, base_url=base_url or settings.redirect_base)


def session_nonce() -> str:
    return uid()
