"""Admin console (server-rendered pages, no npm build) + the agent-org claim flow."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import (ApiKey, AuditEntry, Budget, GatewayModel, LinkToken, LinkedAccount,
                      Org, RegisteredUser, RoutingPolicy, SecurityRule, Ticket, Contact, Deal,
                      ToolAccessRequest, ToolPack, UsageLog, WebhookDelivery, WebhookEndpoint)
from ..security import admin_cookie_ok, create_api_key
from ..metering import audit
from ..utils import iso, uid, new_token
from ..events import emit

router = APIRouter(tags=["dashboard"])
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=TEMPLATE_DIR)


def require_admin(request: Request, db: Session = Depends(get_db)) -> Org:
    if not admin_cookie_ok(request):
        raise HTTPException(401, {"detail": "Admin authentication required. POST /login with the admin token."})
    org = (db.query(Org).filter(Org.kind == "core").order_by(Org.created_at).first())
    if not org:
        raise HTTPException(500, {"detail": "No core org seeded. Restart the platform to re-seed."})
    return org


def page(request, template: str, extra: dict):
    return templates.TemplateResponse(request, template, extra)

# ---------------------------------------------------------------- login


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@router.post("/login")
def login(request: Request, token: str = Form(...)):
    if token.strip() == settings.admin_token:
        response = RedirectResponse("/dashboard", status_code=303)
        response.set_cookie("cx_admin", settings.admin_cookie_value, httponly=True, samesite="lax")
        return response
    return templates.TemplateResponse(request, "login.html",
                                      {"error": "Wrong admin token."}, status_code=401)


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("cx_admin")
    return response

# ---------------------------------------------------------------- overview


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def overview(request: Request, db: Session = Depends(get_db)):
    if not admin_cookie_ok(request):
        return RedirectResponse("/login", status_code=303)
    org = require_admin(request, db)
    linked = db.query(LinkedAccount).filter(LinkedAccount.org_id == org.id).count()
    tickets = db.query(Ticket).count()
    contacts = db.query(Contact).count()
    deals = db.query(Deal).count()
    agents = db.query(Org).filter(Org.kind == "agent").count()
    models = db.query(GatewayModel).count()
    usage_rows = db.query(UsageLog).order_by(UsageLog.ts.desc()).limit(8).all()
    return page(request, "index.html", {"org": org, "linked": linked, "tickets": tickets,
                                        "contacts": contacts, "deals": deals, "agents": agents,
                                        "models": models, "usage_rows": usage_rows, "iso": iso})

# ---------------------------------------------------------------- keys


@router.get("/dashboard/keys", response_class=HTMLResponse)
def keys_page(request: Request, db: Session = Depends(get_db), created: str = ""):
    if not admin_cookie_ok(request):
        return RedirectResponse("/login", status_code=303)
    org = require_admin(request, db)
    rows = (db.query(ApiKey).filter(ApiKey.org_id == org.id)
            .order_by(ApiKey.created_at.desc()).all())
    return page(request, "keys.html", {"org": org, "keys": rows, "iso": iso, "created_key": created})


@router.post("/dashboard/keys/create")
async def create_key(request: Request, db: Session = Depends(get_db),
                     name: str = Form(...), scope: str = Form("data")):
    org = require_admin(request, db)
    if scope not in ("data", "gateway", "admin"):
        scope = "data"
    key = create_api_key(db, org=org, name=name.strip() or "key", scope=scope)
    audit(db, event_type="API_KEY_CREATED", description=f"{scope}:{key.name}", org_id=org.id, actor="dashboard")
    return RedirectResponse(f"/dashboard/keys?created={key.key_secret}", status_code=303)


@router.post("/dashboard/keys/revoke")
async def revoke_key(request: Request, key_id: str = Form(...), db: Session = Depends(get_db)):
    org = require_admin(request, db)
    key = db.query(ApiKey).filter(ApiKey.id == key_id, ApiKey.org_id == org.id).first()
    if key:
        key.active = False
        db.commit()
        audit(db, event_type="API_KEY_REVOKED", description=key.name, org_id=org.id, actor="dashboard")
    return RedirectResponse("/dashboard/keys", status_code=303)

# ---------------------------------------------------------------- link + linked accounts


@router.get("/dashboard/link", response_class=HTMLResponse)
def link_page_console(request: Request, db: Session = Depends(get_db), created: str = ""):
    if not admin_cookie_ok(request):
        return RedirectResponse("/login", status_code=303)
    org = require_admin(request, db)
    accounts = (db.query(LinkedAccount).filter(LinkedAccount.org_id == org.id)
                .order_by(LinkedAccount.linked_at.desc()).all())
    tokens = (db.query(LinkToken).filter(LinkToken.org_id == org.id)
              .order_by(LinkToken.created_at.desc()).limit(8).all())
    return page(request, "link.html", {"org": org, "accounts": accounts, "tokens": tokens,
                                       "iso": iso, "created_link": created})


@router.post("/dashboard/link/create-token")
async def create_link_token(request: Request, categories: str = Form("ticketing,crm"),
                            db: Session = Depends(get_db)):
    org = require_admin(request, db)
    cats = [c.strip() for c in categories.split(",") if c.strip() in ("ticketing", "crm")][:2]
    if not cats:
        cats = ["ticketing"]
    token = LinkToken(id=uid(), org_id=org.id, token=new_token("cxl"),
                      categories=cats)
    db.add(token)
    db.commit()
    audit(db, event_type="LINK_TOKEN_CREATED", description=token.token, org_id=org.id, actor="dashboard")
    return RedirectResponse(f"/dashboard/link?created={token.token}", status_code=303)

# ---------------------------------------------------------------- webhooks


@router.get("/dashboard/webhooks", response_class=HTMLResponse)
def webhooks_page(request: Request, db: Session = Depends(get_db)):
    if not admin_cookie_ok(request):
        return RedirectResponse("/login", status_code=303)
    org = require_admin(request, db)
    endpoints = db.query(WebhookEndpoint).filter(WebhookEndpoint.org_id == org.id).all()
    deliveries = (db.query(WebhookDelivery).filter(WebhookDelivery.id.isnot(None))
                  .order_by(WebhookDelivery.created_at.desc()).limit(15).all())
    return page(request, "webhooks.html", {"org": org, "endpoints": endpoints,
                                           "deliveries": deliveries, "iso": iso})


@router.post("/dashboard/webhooks/create")
async def create_webhook(request: Request, url: str = Form(...), events: str = Form("*"),
                         db: Session = Depends(get_db)):
    org = require_admin(request, db)
    endpoint = WebhookEndpoint(id=uid(), org_id=org.id, url=url.strip(),
                               secret=new_token("whsec", 16),
                               events=[e.strip() for e in events.split(",") if e.strip()])
    db.add(endpoint)
    db.commit()
    audit(db, event_type="WEBHOOK_ENDPOINT_CREATED", description=url, org_id=org.id, actor="dashboard")
    return RedirectResponse("/dashboard/webhooks", status_code=303)


@router.post("/dashboard/webhooks/send-test")
def send_test_webhook(request: Request, endpoint_id: str = Form(...),
                      background=None, db: Session = Depends(get_db)):
    org = require_admin(request, db)
    from fastapi import BackgroundTasks
    background = BackgroundTasks()
    emit(db, background, event="Test.connected", org_id=org.id, payload_data={"ping": True})
    return RedirectResponse("/dashboard/webhooks", status_code=303)

# ---------------------------------------------------------------- agents


@router.get("/dashboard/agents", response_class=HTMLResponse)
def agents_page(request: Request, db: Session = Depends(get_db)):
    if not admin_cookie_ok(request):
        return RedirectResponse("/login", status_code=303)
    org = require_admin(request, db)
    agent_orgs = db.query(Org).filter(Org.kind == "agent").order_by(Org.created_at.desc()).limit(15).all()
    packs = {}
    users = {}
    for agent_org in agent_orgs:
        packs[agent_org.id] = db.query(ToolPack).filter(ToolPack.agent_org_id == agent_org.id).all()
        users[agent_org.id] = db.query(RegisteredUser).filter(RegisteredUser.agent_org_id == agent_org.id).count()
    requests = (db.query(ToolAccessRequest).order_by(ToolAccessRequest.created_at.desc())
                .limit(12).all())
    audit_rows = db.query(AuditEntry).order_by(AuditEntry.ts.desc()).limit(20).all()
    return page(request, "agents.html", {"agent_orgs": agent_orgs, "packs": packs, "users": users,
                                         "requests": requests, "audit_rows": audit_rows, "iso": iso})


@router.post("/dashboard/agents/requests/resolve")
async def resolve_request(request: Request, request_id_row: str = Form(...),
                          decision: str = Form(...), db: Session = Depends(get_db)):
    org = require_admin(request, db)
    row = db.query(ToolAccessRequest).filter(ToolAccessRequest.id == request_id_row).first()
    if row and row.status == "pending":
        row.status = "approved" if decision == "approve" else "rejected"
        row.reviewer_note = f"Decided from dashboard by {org.name}"
        db.commit()
        audit(db, event_type="TOOL_ACCESS_RESOLVED",
              description=f"{row.tool_name} -> {row.status}", org_id=org.id, actor="dashboard")
    return RedirectResponse("/dashboard/agents", status_code=303)

# ---------------------------------------------------------------- gateway console


@router.get("/dashboard/gateway", response_class=HTMLResponse)
def gateway_page(request: Request, db: Session = Depends(get_db)):
    if not admin_cookie_ok(request):
        return RedirectResponse("/login", status_code=303)
    org = require_admin(request, db)
    models = db.query(GatewayModel).all()
    policies = db.query(RoutingPolicy).filter(RoutingPolicy.org_id == org.id).all()
    budget = db.query(Budget).filter(Budget.org_id == org.id).first()
    usage_rows = (db.query(UsageLog).filter(UsageLog.org_id == org.id, UsageLog.plane == "gateway")
                  .order_by(UsageLog.ts.desc()).limit(12).all())
    return page(request, "gateway.html", {"org": org, "models": models, "policies": policies,
                                          "budget": budget, "usage_rows": usage_rows, "iso": iso})


@router.post("/dashboard/gateway/policy-default")
async def policy_default(request: Request, policy_id: str = Form(...), db: Session = Depends(get_db)):
    org = require_admin(request, db)
    db.query(RoutingPolicy).filter(RoutingPolicy.org_id == org.id).update({"is_default": False})
    row = db.query(RoutingPolicy).filter(RoutingPolicy.id == policy_id, RoutingPolicy.org_id == org.id).first()
    if row:
        row.is_default = True
        audit(db, event_type="ROUTING_POLICY_DEFAULT", description=row.name, org_id=org.id, actor="dashboard")
    db.commit()
    return RedirectResponse("/dashboard/gateway", status_code=303)


@router.post("/dashboard/gateway/budget")
async def set_budget(request: Request, credits_total: int = Form(...), db: Session = Depends(get_db)):
    org = require_admin(request, db)
    budget = db.query(Budget).filter(Budget.org_id == org.id).first()
    if not budget:
        budget = Budget(id=uid(), org_id=org.id, credits_total=max(1, credits_total))
        db.add(budget)
    else:
        budget.credits_total = max(1, credits_total)
    db.commit()
    audit(db, event_type="BUDGET_UPDATED", description=f"{credits_total} credits/month", org_id=org.id,
          actor="dashboard")
    return RedirectResponse("/dashboard/gateway", status_code=303)

# ---------------------------------------------------------------- agent org claim flow


@router.get("/claim/{claim_token}", response_class=HTMLResponse)
def claim_page(request: Request, claim_token: str, db: Session = Depends(get_db)):
    org = db.query(Org).filter(Org.claim_token == claim_token).first()
    admin_ok = admin_cookie_ok(request)
    return templates.TemplateResponse(request, "claim.html",
                                      {"agent_org": org, "admin_ok": admin_ok,
                                       "claim_token": claim_token})


@router.post("/claim/{claim_token}")
async def claim(request: Request, claim_token: str, db: Session = Depends(get_db)):
    if not admin_cookie_ok(request):
        return RedirectResponse(f"/claim/{claim_token}", status_code=303)
    agent_org = db.query(Org).filter(Org.claim_token == claim_token).first()
    if not agent_org:
        raise HTTPException(404, "Unknown claim token.")
    core_org = db.query(Org).filter(Org.kind == "core").order_by(Org.created_at).first()
    agent_org.claimed = True
    agent_org.settings = {**(agent_org.settings or {}), "claimed_by": core_org.id}
    agent_org.claim_token = ""
    db.commit()
    audit(db, event_type="AGENT_ORG_CLAIMED", description=f"{agent_org.slug} claimed by {core_org.name}",
          org_id=core_org.id, agent_org_id=agent_org.id, actor="dashboard")
    return RedirectResponse("/dashboard/agents", status_code=303)
