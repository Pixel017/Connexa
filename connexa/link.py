"""Link plane — short-lived link tokens, the end-user connection UI,
and credential capture. This is the human half of every connection.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

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


# ---------------------------------------------------------------- dashboard API

class LinkTokenRequest(BaseModel):
    categories: list[str] = Field(default_factory=lambda: ["ticketing", "crm"])
    integration: str | None = None


@api_router.post("/link-tokens")
def create_link_token(data: LinkTokenRequest, auth=Depends(data_plane_key), db: Session = Depends(get_db)):
    key, org = auth
    for category in data.categories:
        if category not in ("ticketing", "crm"):
            raise HTTPException(400, {"detail": f"Unsupported category '{category}'."})
    token = LinkToken(id=uid(), org_id=org.id, token=new_token("cxl"), categories=data.categories,
                      integration=data.integration)
    db.add(token)
    db.commit()
    audit(db, event_type="LINK_TOKEN_CREATED", description=token.token, org_id=org.id, key_id=key.id)
    return {"link_token": token.token,
            "expires_at": iso(token.expires_at),
            "url": f"/link/{token.token}",
            "categories": token.categories}


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
    })


class ConnectRequest(BaseModel):
    integration: str
    end_user_name: str = Field(min_length=1)


RESPONSE_TOKENS: dict[str, str] = {}


@router.post("/{token_value}/connect")
def connect(token_value: str, data: ConnectRequest, background: BackgroundTasks,
            request: Request, db: Session = Depends(get_db)):
    token = db.query(LinkToken).filter(LinkToken.token == token_value).first()
    if not token or token.status != "ready" or token.expires_at < naive_now():
        raise HTTPException(410, {"detail": "Link token expired or already used."})
    connector = registry.get(data.integration)
    if not connector or not connector.generate_sample("ticketing") and not connector.generate_sample("crm"):
        raise HTTPException(400, {"detail": f"'{data.integration}' is not a connectable integration."})
    category = "ticketing" if connector.generate_sample("ticketing") else "crm"
    if category not in token.categories:
        raise HTTPException(400, {"detail": f"Integration '{data.integration}' is outside this link token's categories."})

    account_token = new_token("cxt")
    account = LinkedAccount(id=uid(), org_id=token.org_id, category=category,
                            integration=connector.slug, end_user_name=data.end_user_name,
                            account_token=account_token, credentials={})
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
