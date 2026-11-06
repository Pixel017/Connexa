"""Public OAuth routes — the "social login" lane.

Two endpoints:
- GET /api/v1/registered-users/{ru}/oauth/{slug}/start  (agent key) — returns a signed
  authorization URL for a human (also reachable via the authenticate_<slug> MCP tool).
- GET /oauth/{slug}/callback  (browser, no auth) — the provider redirects here; state is
  verified (HMAC-signed, binds org+user+connector, 15-minute expiry), the code is
  exchanged against the provider, and credentials are stored on the registered user.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .agent.handler import agent_key
from .connectors.base import OAuthExchangeError
from .connectors.registry import registry
from .db import get_db
from .metering import audit
from .models import RegisteredUser
from .oauth import build_authorize_url, parse_state
from .security import require_scope

router = APIRouter()

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "dashboard", "templates")
templates = Jinja2Templates(directory=TEMPLATE_DIR)


@router.get("/api/v1/registered-users/{registered_user_id}/oauth/{slug}/start")
def start_oauth(registered_user_id: str, slug: str,
                auth=Depends(require_scope("agent")), db: Session = Depends(get_db)):
    key, org = auth
    connector = registry.get(slug)
    if not connector:
        raise HTTPException(404, {"detail": f"Unknown connector '{slug}'."})
    user = db.query(RegisteredUser).filter(RegisteredUser.id == registered_user_id,
                                           RegisteredUser.agent_org_id == org.id).first()
    if not user:
        raise HTTPException(404, {"detail": "Unknown registered user for this agent org."})
    if not connector.oauth_ready:
        return {
            "connector": slug, "status": "demo_lane", "oauth_configured": False,
            "connected": bool((user.credentials or {}).get(slug)),
            "note": (f"OAuth for {connector.name} is not configured on this deployment. "
                     f"Call authenticate_{slug} on the MCP surface to use the demo lane, or set "
                     f"CONNEXA_{slug.upper()}_CLIENT_ID / _CLIENT_SECRET in .env to enable live sign-in."),
        }
    url = build_authorize_url(connector, base_url="", agent_org_id=org.id,
                              registered_user_id=user.id)
    return {"connector": slug, "status": "authorization_required", "oauth_configured": True,
            "authorization_url": url, "expires_in": 900,
            "callback_path": connector.oauth_redirect_path,
            "note": "Open this URL in a browser; a human approves, then the token lands on this registered user."}


@router.get("/oauth/{slug}/callback", response_class=HTMLResponse)
def oauth_callback(slug: str, request: Request, code: str = "",
                   error: str = "", error_description: str = "", state: str = "",
                   db: Session = Depends(get_db)):
    connector = registry.get(slug)

    def done(ok: bool, heading_reason: str, detail: str = "") -> HTMLResponse:
        return templates.TemplateResponse(request, "oauth_done.html", {
            "ok": ok, "reason": heading_reason, "detail": detail,
            "connector_name": connector.name if connector else slug,
            "agent_surface": "/api/v1/agent/signup/",
        })

    if error:
        detail = error_description or ""
        audit(db, event_type="AGENT_OAUTH_FAILED", description=f"{slug}: provider said {error}",
              agent_org_id=None)
        return done(False, f"Authorization denied — provider returned '{error}'.", detail)
    if not connector:
        return done(False, "Unknown connector.", "No connector with this slug is registered.")
    st = parse_state(state)
    if not st or st.get("slug") != slug:
        return done(False, "This authorization link is invalid or expired.",
                    "Ask the agent to call authenticate again for a fresh URL.")
    user = db.query(RegisteredUser).filter(RegisteredUser.id == st.get("ru")).first()
    if not user or user.agent_org_id != st.get("org"):
        return done(False, "The user this authorization belongs to no longer matches.", "")
    if not connector.oauth_ready:
        return done(False, "OAuth is not configured for this connector on this deployment.", "")
    if not code:
        return done(False, "No authorization code came back from the provider.", "")

    redirect_uri = connector.redirect_uri(str(request.base_url).rstrip("/"))
    try:
        granted = connector.exchange_code(code, redirect_uri)
    except OAuthExchangeError as exc:
        db.rollback()
        audit(db, event_type="AGENT_OAUTH_FAILED", description=f"{slug}: {exc}",
              agent_org_id=user.agent_org_id)
        return done(False, "Token exchange failed.", str(exc))

    user.credentials = {**(user.credentials or {}), slug: {"type": "oauth", **granted}}
    db.commit()
    audit(db, event_type="AGENT_OAUTH_CONNECTED",
          description=f"{connector.name} connected for {user.origin_user_name}"
                      + (f" ({granted.get('account_label')})" if granted.get("account_label") else ""),
          actor="oauth",
          agent_org_id=user.agent_org_id)
    return done(True,
                f"{connector.name} connected" + (f" as {granted.get('account_label')}"
                                                 if granted.get("account_label") else ""),
                "")
