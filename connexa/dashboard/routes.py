"""Admin console (server-rendered pages, no npm build) + the agent-org claim flow."""
from __future__ import annotations

import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import settings
from ..connectors.registry import registry
from ..db import get_db
from ..models import (ApiKey, AuditEntry, Budget, GatewayModel, LinkToken, LinkedAccount,
                      Org, RegisteredUser, RoutingPolicy, SecurityRule, Skill, SyncIssue,
                      TeamMembership, Ticket, Contact, Deal, ToolAccessRequest, ToolPack,
                      UsageLog, WebhookDelivery, WebhookEndpoint, WorkforceMemory, WorkforceTeam)
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
def root_redirect():
    # `/` belongs to the public marketing site; consoles live under /dashboard.
    return RedirectResponse("/dashboard", status_code=303)


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


# ---------------------------------------------------------------- linked accounts + sync

@router.get("/dashboard/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, db: Session = Depends(get_db), flash: str = ""):
    if not admin_cookie_ok(request):
        return RedirectResponse("/login", status_code=303)
    org = require_admin(request, db)
    accounts = (db.query(LinkedAccount).filter(LinkedAccount.org_id == org.id)
                .order_by(LinkedAccount.linked_at.desc()).all())
    for account in accounts:
        account.tickets_n = db.query(Ticket).filter(Ticket.linked_account_id == account.id).count()
        account.contacts_n = db.query(Contact).filter(Contact.linked_account_id == account.id).count()
        account.deals_n = db.query(Deal).filter(Deal.linked_account_id == account.id).count()
        account.open_issues = (db.query(SyncIssue)
                               .filter(SyncIssue.linked_account_id == account.id, SyncIssue.resolved == False)  # noqa: E712
                               .count())
        connector = registry.get(account.integration)
        account.connector_name = connector.name if connector else account.integration
    return page(request, "accounts.html", {"org": org, "accounts": accounts, "iso": iso, "flash": flash})


@router.post("/dashboard/accounts/frequency")
async def accounts_frequency(request: Request, db: Session = Depends(get_db),
                             account_id: str = Form(...), seconds: int = Form(...)):
    org = require_admin(request, db)
    account = (db.query(LinkedAccount)
               .filter(LinkedAccount.id == account_id, LinkedAccount.org_id == org.id).first())
    if account:
        seconds = max(0, min(int(seconds), 86_400 * 7))
        account.sync_frequency_seconds = seconds
        db.commit()
        audit(db, event_type="SYNC_SCHEDULE_SET", org_id=org.id, actor="dashboard",
              description=(f"{account.integration}: every {seconds}s" if seconds
                           else f"{account.integration}: manual only"))
        flash = (f"Scheduled {account.integration}: every {seconds}s" if seconds
                 else f"{account.integration}: scheduled sync off")
    else:
        flash = "Unknown linked account."
    return RedirectResponse(f"/dashboard/accounts?flash={quote(flash)}", status_code=303)


@router.post("/dashboard/accounts/sync")
async def accounts_sync(request: Request, account_id: str = Form(...), db: Session = Depends(get_db)):
    org = require_admin(request, db)
    account = (db.query(LinkedAccount)
               .filter(LinkedAccount.id == account_id, LinkedAccount.org_id == org.id).first())
    if account:
        from ..sync import run_sync
        summary = run_sync(db, account, actor="dashboard")
        flash = (f"Synced {account.integration}: +{summary['added']} added, "
                 f"~{summary['updated']} updated, ={summary['unchanged']} unchanged")
    else:
        flash = "Unknown linked account."
    return RedirectResponse(f"/dashboard/accounts?flash={quote(flash)}", status_code=303)


# ---------------------------------------------------------------- audit trail

@router.get("/dashboard/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db)):
    if not admin_cookie_ok(request):
        return RedirectResponse("/login", status_code=303)
    org = require_admin(request, db)
    rows = db.query(AuditEntry).order_by(AuditEntry.ts.desc()).limit(100).all()
    total = db.query(AuditEntry).count()
    return page(request, "audit.html", {"org": org, "rows": rows, "total": total, "iso": iso})


# ---------------------------------------------------------------- workforce governance

@router.get("/dashboard/workforce", response_class=HTMLResponse)
def workforce_page(request: Request, db: Session = Depends(get_db), flash: str = ""):
    if not admin_cookie_ok(request):
        return RedirectResponse("/login", status_code=303)
    org = require_admin(request, db)
    teams = db.query(WorkforceTeam).filter(WorkforceTeam.org_id == org.id).order_by(WorkforceTeam.name).all()
    team_rows = []
    for team in teams:
        memberships = db.query(TeamMembership).filter(TeamMembership.team_id == team.id).all()
        members = []
        for membership in memberships:
            user = db.query(RegisteredUser).filter(RegisteredUser.id == membership.registered_user_id).first()
            members.append({"id": membership.registered_user_id,
                            "name": user.origin_user_name if user else membership.registered_user_id,
                            "active": bool(user.active) if user else True})
        pack = db.query(ToolPack).filter(ToolPack.id == team.tool_pack_id).first() if team.tool_pack_id else None
        team_rows.append({"team": team, "members": members,
                          "pack": pack.name if pack else None,
                          "member_count": len(members)})
    skills = db.query(Skill).filter(Skill.org_id == org.id).order_by(Skill.name).all()
    memories = (db.query(WorkforceMemory).filter(WorkforceMemory.org_id == org.id)
                .order_by(WorkforceMemory.created_at.desc()).limit(15).all())
    users = db.query(RegisteredUser).filter(RegisteredUser.agent_org_id == org.id).all()
    packs = db.query(ToolPack).filter(ToolPack.agent_org_id == org.id).all()
    return page(request, "workforce.html", {"org": org, "team_rows": team_rows, "skills": skills,
                                            "memories": memories, "users": users, "packs": packs,
                                            "iso": iso, "flash": flash})


@router.post("/dashboard/workforce/teams")
async def create_team_form(request: Request, db: Session = Depends(get_db), name: str = Form(...),
                           tool_pack_id: str = Form(""), allowed_models: str = Form(""),
                           monthly_credit_budget: int = Form(0)):
    org = require_admin(request, db)
    allowlist = [m.strip() for m in (allowed_models or "").split(",") if m.strip()] or ["*"]
    team = WorkforceTeam(id=uid(), org_id=org.id, name=name.strip() or "team",
                         tool_pack_id=tool_pack_id or None,
                         allowed_models=allowlist,
                         monthly_credit_budget=monthly_credit_budget or None)
    db.add(team)
    db.commit()
    audit(db, event_type="TEAM_CREATED", description=team.name, org_id=org.id, actor="dashboard")
    return RedirectResponse("/dashboard/workforce", status_code=303)


@router.post("/dashboard/workforce/teams/{team_id}/members")
async def add_member_form(request: Request, team_id: str, db: Session = Depends(get_db),
                          origin_user_id: str = Form(...)):
    org = require_admin(request, db)
    user = (db.query(RegisteredUser)
            .filter(RegisteredUser.agent_org_id == org.id,
                    (RegisteredUser.id == origin_user_id) | (RegisteredUser.origin_user_id == origin_user_id))
            .first())
    team = db.query(WorkforceTeam).filter(WorkforceTeam.id == team_id, WorkforceTeam.org_id == org.id).first()
    if user and team:
        exists = (db.query(TeamMembership)
                  .filter(TeamMembership.team_id == team.id,
                          TeamMembership.registered_user_id == user.id).first())
        if not exists:
            db.add(TeamMembership(id=uid(), team_id=team.id, registered_user_id=user.id))
            db.commit()
            audit(db, event_type="TEAM_MEMBER_ADDED", description=f"{team.name} <- {user.origin_user_name}",
                  org_id=org.id, actor="dashboard")
    return RedirectResponse("/dashboard/workforce", status_code=303)


@router.post("/dashboard/workforce/skills")
async def create_skill_form(request: Request, db: Session = Depends(get_db), name: str = Form(...),
                            instructions: str = Form(...), tags: str = Form("")):
    org = require_admin(request, db)
    skill = Skill(id=uid(), org_id=org.id, name=name.strip() or "skill",
                  instructions=instructions,
                  tags=[t.strip() for t in (tags or "").split(",") if t.strip()])
    db.add(skill)
    db.commit()
    audit(db, event_type="SKILL_PUBLISHED", description=skill.name, org_id=org.id, actor="dashboard")
    return RedirectResponse("/dashboard/workforce", status_code=303)


@router.post("/dashboard/workforce/memories")
async def create_memory_form(request: Request, db: Session = Depends(get_db), content: str = Form(...),
                             tags: str = Form("")):
    org = require_admin(request, db)
    memory = WorkforceMemory(id=uid(), org_id=org.id, author="dashboard", content=content.strip(),
                             tags=[t.strip() for t in (tags or "").split(",") if t.strip()])
    db.add(memory)
    db.commit()
    audit(db, event_type="MEMORY_SAVED", description=memory.content[:80], org_id=org.id, actor="dashboard")
    return RedirectResponse("/dashboard/workforce", status_code=303)

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
