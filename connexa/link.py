"""Link plane — short-lived link tokens, the end-user connection UI,
and credential capture. This is the human half of every connection.
"""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .common_models import DATA_CATEGORIES
from .db import get_db
from .security import data_plane_key
from .models import LinkToken, LinkedAccount, Org
from .metering import audit, usage
from .utils import uid, now, iso, new_token, naive_now
from .events import emit
from .connectors.registry import registry

router = APIRouter(prefix="/link", tags=["link"])
api_router = APIRouter(prefix="/api", tags=["link-api"])

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "dashboard", "templates")
templates = Jinja2Templates(directory=TEMPLATE_DIR)


# ---------------------------------------------------------------- embeddable link component
#
# The hosted link page ships as an embeddable widget: point a <script> at it
# from your product and your end user connects inline (iframe overlay), same
# flow, same trust boundary:
#     <script src="https://your-host/link/embed.js" data-token="cxl_..." async></script>

_EMBED_JS = """(function () {
  'use strict';
  var script = (document.currentScript ||
    document.querySelector('script[src*="link/embed.js"]'));
  if (!script) return;
  var token = script.getAttribute('data-token');
  if (!token) { console.error('connexa-link: data-token attribute is required'); return; }
  var base = (script.getAttribute('data-base') || script.src.split('/link/embed.js')[0]);

  var overlay = document.createElement('div');
  overlay.setAttribute('data-connexa-link', token);
  overlay.style.cssText = 'position:fixed;inset:0;z-index:2147483000;background:rgba(15,18,28,.55);'
    + 'display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity .2s';

  var frame = document.createElement('iframe');
  var linkUrl = base + '/link/' + encodeURIComponent(token);
  frame.src = linkUrl;
  frame.title = 'Connect your account (Connexa Link)';
  frame.style.cssText = 'width:min(560px,94vw);height:min(700px,92vh);border:0;border-radius:16px;'
    + 'box-shadow:0 30px 80px rgba(0,0,0,.35);background:#fff';
  frame.setAttribute('allow', 'clipboard-write');

  var close = document.createElement('button');
  close.textContent = '\\u00d7';
  close.setAttribute('aria-label', 'Close');
  close.style.cssText = 'position:absolute;top:18px;right:22px;width:38px;height:38px;'
    + 'border-radius:50%;border:0;background:#fff;color:#10141c;font-size:20px;cursor:pointer;'
    + 'box-shadow:0 4px 14px rgba(0,0,0,.25)';

  function shut() {
    overlay.style.opacity = '0';
    window.setTimeout(function () { overlay.remove(); }, 220);
  }
  close.addEventListener('click', shut);
  overlay.addEventListener('click', function (event) { if (event.target === overlay) shut(); });

  overlay.appendChild(frame);
  overlay.appendChild(close);
  document.body.appendChild(overlay);
  window.requestAnimationFrame(function () { overlay.style.opacity = '1'; });

  window.addEventListener('message', function (event) {
    var data = event.data || {};
    if (data.source === 'connexa-link' && data.status === 'complete') { shut(); }
  });
})();
"""


@router.get("/embed.js", include_in_schema=False)
def link_embed_js():
    """The self-contained embeddable link component (no build step, no npm)."""
    from fastapi import Response
    return Response(_EMBED_JS, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- dashboard API

class LinkTokenRequest(BaseModel):
    categories: list[str] = Field(default_factory=lambda: ["ticketing", "crm", "hris", "accounting"])
    integration: str | None = None
    # Link flow customization (Merge-Link-style theming):
    company_name: str | None = None
    logo_url: str | None = None
    accent_color: str | None = None     # "#RRGGBB"
    welcome_text: str | None = None


@api_router.post("/link-tokens")
def create_link_token(data: LinkTokenRequest, auth=Depends(data_plane_key), db: Session = Depends(get_db)):
    key, org = auth
    for category in data.categories:
        if category not in DATA_CATEGORIES:
            raise HTTPException(400, {"detail": f"Unsupported category '{category}'."})
    if data.accent_color and not re.fullmatch(r"#[0-9a-fA-F]{6}", data.accent_color):
        raise HTTPException(400, {"detail": "accent_color must look like '#3b5bdb'."})
    token = LinkToken(id=uid(), org_id=org.id, token=new_token("cxl"), categories=data.categories,
                      integration=data.integration,
                      company_name=(data.company_name or "")[:120] or None,
                      logo_url=(data.logo_url or "")[:400] or None,
                      accent_color=data.accent_color,
                      welcome_text=(data.welcome_text or "")[:300] or None)
    db.add(token)
    db.commit()
    audit(db, event_type="LINK_TOKEN_CREATED", description=token.token, org_id=org.id, key_id=key.id)
    return {"link_token": token.token,
            "expires_at": iso(token.expires_at),
            "url": f"/link/{token.token}",
            "categories": token.categories,
            "customize": {"company_name": token.company_name, "logo_url": token.logo_url,
                          "accent_color": token.accent_color, "welcome_text": token.welcome_text}}


@api_router.get("/linked-accounts")
def linked_accounts_and_deliveries(auth=Depends(data_plane_key), db: Session = Depends(get_db)):
    key, org = auth
    rows = db.query(LinkedAccount).filter(LinkedAccount.org_id == org.id).order_by(LinkedAccount.linked_at.desc()).all()
    return {"count": len(rows), "linked_accounts": [
        {"id": r.id, "category": r.category, "integration": r.integration, "status": r.status,
         "end_user_name": r.end_user_name, "linked_at": iso(r.linked_at),
         "account_token_masked": r.account_token[:9] + "\u2026"} for r in rows]}

# ---------------------------------------------------------------- end-user UI


@router.get("/{token_value}", response_class=HTMLResponse)
def link_page(token_value: str, request: Request, db: Session = Depends(get_db)):
    token = db.query(LinkToken).filter(LinkToken.token == token_value).first()
    if not token or token.status != "ready" or token.expires_at < naive_now():
        return templates.TemplateResponse(request, "link_invalid.html", {})
    org = db.get(Org, token.org_id)
    options = {}
    for category in token.categories:
        options[category] = [{"slug": c.slug, "name": c.name, "description": c.description,
                              "real": c.real_lane_ready} for c in registry.data_plane_providers(category)]
    return templates.TemplateResponse(request, "link_page.html", {
        "org_name": org.name if org else "Unknown",
        "token": token_value,
        "options": options,
        "selected_integration": token.integration,
        "custom": {"company_name": token.company_name, "logo_url": token.logo_url,
                   "accent_color": token.accent_color, "welcome_text": token.welcome_text},
    })


class ConnectRequest(BaseModel):
    integration: str
    end_user_name: str = Field(min_length=1)
    # Real-lane integrations: the end user's own provider credential + config.
    credential: str | None = None          # e.g. a GitHub personal access token
    config: dict = Field(default_factory=dict)   # e.g. {"repo": "owner/name"}


RESPONSE_TOKENS: dict[str, str] = {}


# Plan limits enforced at connect time (see /api/v1/billing for the surface).
PLAN_LIMITS: dict[str, dict] = {
    "free": {"linked_accounts": 3},
    "growth": {"linked_accounts": 25},
    "scale": {"linked_accounts": 1_000_000},
}


def _plan_limit_reached(db: Session, org: Org) -> tuple[bool, int]:
    limit = PLAN_LIMITS.get(org.plan or "free", PLAN_LIMITS["free"])["linked_accounts"]
    used = db.query(LinkedAccount).filter(LinkedAccount.org_id == org.id).count()
    return used >= limit, limit


@router.post("/{token_value}/connect")
def connect(token_value: str, data: ConnectRequest, background: BackgroundTasks,
            request: Request, db: Session = Depends(get_db)):
    token = db.query(LinkToken).filter(LinkToken.token == token_value).first()
    if not token or token.status != "ready" or token.expires_at < naive_now():
        raise HTTPException(410, {"detail": "Link token expired or already used."})
    connector = registry.get(data.integration)
    if not connector:
        raise HTTPException(400, {"detail": f"'{data.integration}' is not a connectable integration."})
    real_lane = bool(getattr(connector, "data_categories", []))
    if real_lane:
        category = next((c for c in token.categories if c in connector.data_categories), None)
        if category is None:
            raise HTTPException(400, {"detail": (f"Integration '{data.integration}' is outside this "
                                                 f"link token's categories.")})
        if not data.credential and not getattr(connector, "env_token", ""):
            raise HTTPException(400, {"detail": (f"'{data.integration}' is a REAL integration — pass your "
                                                 f"provider credential in 'credential'.")})
        if not (data.config or {}).get("repo"):
            raise HTTPException(400, {"detail": "A repository is required: config = {\"repo\": \"owner/name\"}."})
    else:
        category = next((c for c in DATA_CATEGORIES if connector.generate_sample(c)), None)
        if category is None:
            raise HTTPException(400, {"detail": f"'{data.integration}' has no data-plane source."})
        if category not in token.categories:
            raise HTTPException(400, {"detail": f"Integration '{data.integration}' is outside this link token's categories."})

    org = db.get(Org, token.org_id)
    limit_hit, limit = _plan_limit_reached(db, org)
    if limit_hit:
        raise HTTPException(402, {"error": {
            "code": "plan_limit_reached",
            "message": (f"The {org.plan} plan allows {limit} linked accounts. "
                        f"Upgrade to connect more."),
            "plan": org.plan, "limit": limit}})

    credentials: dict = {}
    if real_lane:
        credentials = {"type": "user", "repo": data.config.get("repo")}
        if data.credential:
            credentials["token"] = data.credential

    account_token = new_token("cxt")
    account = LinkedAccount(id=uid(), org_id=token.org_id, category=category,
                            integration=connector.slug, end_user_name=data.end_user_name,
                            account_token=account_token, credentials=credentials)
    db.add(account)

    # Initial pull: provider -> Common Model sync (same engine as every later sync).
    from .sync import run_sync
    summary = run_sync(db, account, background=background, actor="link")

    token.status = "used"
    db.commit()

    audit(db, event_type="LINKED_ACCOUNT_CREATED",
          description=f"{category}/{connector.slug}: initial sync +{summary['added']}",
          org_id=token.org_id)
    usage(db, plane="data", kind="initial_sync", org_id=token.org_id, units=1.0, credits=0)
    emit(db, background, event="LinkedAccount.linked", org_id=token.org_id, payload_data={
        "linked_account_id": account.id, "category": category,
        "integration": connector.slug, "end_user_name": data.end_user_name})
    if "text/html" in (request.headers.get("accept") or ""):
        return RedirectResponse(f"/link/{token_value}/done?pt={account_token}", status_code=303)
    return {"public_token": account_token, "category": category,
            "integration": connector.slug,
            "next_step": (f"POST /api/{category}/v1/account-token/{account_token} with your data API key "
                          f"to finish the exchange, then call data endpoints with X-Account-Token.")}


@router.get("/{token_value}/done", response_class=HTMLResponse)
def link_done(token_value: str, request: Request, pt: str = "", db: Session = Depends(get_db)):
    token = db.query(LinkToken).filter(LinkToken.token == token_value).first()
    return templates.TemplateResponse(request, "link_done.html", {"token": token_value, "pt": pt})
