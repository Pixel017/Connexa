"""Connector framework: every provider — simulated or real — implements this interface.

OAuth 2.0 "social login" support lives here too: a connector that has been
configured with client credentials (via env → registry) serves a real authorize
URL from its `authenticate` meta-tool; a human completes consent in the browser;
the callback (`connexa.oauth_routes`) exchanges the code and stores per-user
credentials which every later tool call resolves through `credential_token`.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel

from ..utils import naive_now


class OAuthExchangeError(Exception):
    """Raised when a provider rejects an authorization-code (or refresh) exchange."""


@dataclass
class ToolSpec:
    """One agent-callable tool. Schema style: a single {"input": {...}} indirection
    so heterogeneous providers present a uniform shape to agents."""
    name: str
    description: str
    handler: Callable[[ToolContext, Any], Any]
    input_model: type[BaseModel] | None = None
    credit_type: str = "low"
    annotations: dict = field(default_factory=lambda: {
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": False,
    })
    needs_credentials: bool = True


@dataclass
class ToolContext:
    # Per-call context handed to every tool handler.
    connector_slug: str
    credentials: dict                      # for THIS registered user (may be {} = not connected)
    env_token: str = ""                    # platform-wide token for the real lane, if provided
    registered_user_id: str | None = None
    agent_org_id: str | None = None
    base_url: str = ""                     # request host base — used to build OAuth redirect URIs
    persist_credentials: Callable[[dict], None] = lambda c: None   # call to save credentials


def credential_token(ctx: ToolContext) -> str:
    """Best provider token for this call.

    Per-account credentials (an OAuth grant or a user-pasted token) win over the
    platform-wide env token: they were attached to exactly this registered user.
    """
    creds = ctx.credentials or {}
    cred_type = creds.get("type")
    if cred_type == "oauth":
        token = creds.get("access_token") or creds.get("bot_token") or ""
        if token:
            return str(token)
    if cred_type == "user" and creds.get("token"):
        return str(creds["token"])
    if ctx.env_token:
        return str(ctx.env_token)
    return str(creds.get("token") or "")


NO_AUTH_SCHEMA = {"type": "object", "required": ["input"],
                  "properties": {"input": {"type": "object", "title": "Input", "additionalProperties": True}},
                  "additionalProperties": False}


class Connector:
    """Base connector. Subclasses define tools via build_tools()."""

    slug: str = "abstract"
    name: str = "abstract connector"
    description: str = ""
    auth_type: str = "token"     # none | token | oauth (set dynamically by configure_oauth)
    real_lane_ready: bool = False  # True when a real provider token is configured

    # --- OAuth 2.0 configuration (populated per connector via configure_oauth) ----------
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_scopes: str = ""          # provider-formatted: "repo read:user" / "chat:write,channels:read" / ""
    oauth_extra_auth_params: dict = {}   # provider extras for the consent URL
    oauth_authorize_url: str = ""   # provider consent screen
    oauth_token_url: str = ""       # provider token endpoint
    oauth_redirect_path: str = ""

    def __init__(self) -> None:
        self.oauth_redirect_path = f"/oauth/{self.slug}/callback"
        self.tools: list[ToolSpec] = list(self.build_tools()) + self.meta_tools()

    # --- OAuth surface -----------------------------------------------------------------
    @property
    def oauth_ready(self) -> bool:
        return bool(self.oauth_client_id and self.oauth_client_secret
                    and self.oauth_authorize_url and self.oauth_token_url)

    def configure_oauth(self, *, client_id: str, client_secret: str, scopes: str = "",
                        extra_auth_params: dict | None = None) -> None:
        self.oauth_client_id = client_id
        self.oauth_client_secret = client_secret
        self.oauth_scopes = scopes
        self.oauth_extra_auth_params = dict(extra_auth_params or {})
        self.auth_type = "oauth"

    def redirect_uri(self, base_url: str) -> str:
        return f"{(base_url or '').rstrip('/')}{self.oauth_redirect_path}"

    def authorize_url(self, *, state: str, base_url: str) -> str:
        params = {"client_id": self.oauth_client_id, "redirect_uri": self.redirect_uri(base_url),
                  "response_type": "code", "state": state}
        if self.oauth_scopes:
            params["scope"] = self.oauth_scopes
        params.update(self.oauth_extra_auth_params)
        return f"{self.oauth_authorize_url}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        """Standard OAuth 2.0 authorization-code exchange; providers with unusual
        shapes (Slack/Notion) override this."""
        response = httpx.post(self.oauth_token_url,
                              data={"grant_type": "authorization_code", "code": code,
                                    "redirect_uri": redirect_uri,
                                    "client_id": self.oauth_client_id,
                                    "client_secret": self.oauth_client_secret},
                              headers={"Accept": "application/json"}, timeout=15.0)
        return self._parse_exchange(response.json())

    def refresh_token(self, refresh_token: str) -> dict:
        response = httpx.post(self.oauth_token_url,
                              data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                                    "client_id": self.oauth_client_id,
                                    "client_secret": self.oauth_client_secret},
                              headers={"Accept": "application/json"}, timeout=15.0)
        return self._parse_exchange(response.json())

    def _parse_exchange(self, data: dict) -> dict:
        token = data.get("access_token")
        if not token:
            reason = data.get("error_description") or data.get("error") or "provider_exchange_failed"
            raise OAuthExchangeError(str(reason))
        creds: dict = {"access_token": token, "token_type": data.get("token_type", "bearer")}
        if data.get("refresh_token"):
            creds["refresh_token"] = data["refresh_token"]
        if data.get("expires_in"):
            creds["expires_at"] = (naive_now() + datetime.timedelta(seconds=int(data["expires_in"]))).isoformat()
        creds["account_label"] = self.describe_token(token)
        return creds

    def describe_token(self, token: str) -> str | None:
        """Optionally probe the provider for a human account label (workspace name, login...)."""
        return None

    def maybe_refresh(self, creds: dict) -> dict | None:
        """Return refreshed credentials if they are oauth-type, expiring/expired and refreshable."""
        if creds.get("type") != "oauth" or not creds.get("refresh_token") or not creds.get("expires_at"):
            return None
        try:
            expired = datetime.datetime.fromisoformat(str(creds["expires_at"])) <= naive_now()
        except ValueError:
            return None
        if not expired:
            return None
        refreshed = self.refresh_token(creds["refresh_token"])
        return {"type": "oauth", **refreshed}

    # --- standard meta-tools appended to every connector's tool list -------------
    def meta_tools(self) -> list[ToolSpec]:
        spec = self
        needs = spec.auth_type != "none"

        def authenticate(ctx: ToolContext, _input: Any) -> Any:
            if spec.oauth_ready:
                from ..oauth import build_authorize_url
                url = build_authorize_url(spec, base_url=ctx.base_url,
                                          agent_org_id=ctx.agent_org_id or "",
                                          registered_user_id=ctx.registered_user_id or "")
                return {"connector": spec.slug, "status": "authorization_required",
                        "authorization_url": url, "expires_in": 900,
                        "note": ("A human must open this URL in a browser and approve access. "
                                 "After the provider redirects back to Connexa, credentials are "
                                 "stored automatically for this registered user — then re-call "
                                 f"{spec.slug}__validate_credential and the connector's tools.")}
            if ctx.env_token:
                ctx.credentials["type"] = "platform"
                ctx.credentials["token"] = ctx.env_token
                persist = {"type": "platform"}
            else:
                ctx.credentials["type"] = "demo"
                persist = {"type": "demo"}
            ctx.persist_credentials(persist)
            return {
                "connector": spec.slug,
                "status": "connected",
                "mode": "platform-token" if ctx.env_token else "demo",
                "note": ("Live provider token configured." if ctx.env_token
                         else "Demo mode: synthetic credentials granted. No real provider reached."),
            }

        def validate_credential(ctx: ToolContext, _input: Any) -> Any:
            if spec.auth_type == "none":
                return {"connector": spec.slug, "status": "no_credentials_required"}
            if not ctx.credentials:
                return {"error_reason": "reauth_required",
                        "error_message": f"Your credentials are missing or invalid. Call the authenticate_{spec.slug} tool to reauthenticate.",
                        "data": {"tool_name": f"{spec.slug}__validate_credential",
                                 "reauth_reason": "not_connected",
                                 "authenticate_tool": f"authenticate_{spec.slug}"},
                        "error_code": 401}
            label = ctx.credentials.get("account_label")
            return {"connector": spec.slug, "status": "validated",
                    "mode": ctx.credentials.get("type", "unknown"),
                    **({"account": label} if label else {})}

        tools: list[ToolSpec] = []
        tools.append(ToolSpec(
            name=f"validate_credential", description=f"Check whether {spec.name} is connected and credentials are valid.",
            handler=validate_credential, input_model=None, credit_type="low",
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        ))
        tools.append(ToolSpec(
            name=f"authenticate", description=f"Run the credential sign-in flow for {spec.name} so tool calls on behalf of this user work.",
            handler=authenticate, input_model=None, credit_type="low",
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        ))
        if not needs:
            for t in tools:
                t.needs_credentials = False
        return tools

    # --- simulation hooks -------------------------------------------------------
    def generate_sample(self, category: str) -> list[dict]:  # noqa: ARG002
        return []

    def passthrough(self, ctx: ToolContext, method: str, path: str) -> dict:  # noqa: ARG002
        return {"status": 404, "body": {"error": f"{self.slug}: no provider route for {method} {path}"}}
