"""Agent Handler REST surface.

Documented agent bootstrap:
    POST /api/v1/agent/signup/   (no auth) -> org + API key + starter ToolPack + registered user + claim link
Everything else requires an agent-scoped Bearer key.
"""
from __future__ import annotations

import csv
import datetime
import io
import re
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (ApiKey, Org, RegisteredUser, SecurityRule, ToolAccessRequest, ToolPack, AuditEntry)
from ..security import require_scope, create_api_key
from ..metering import audit
from ..utils import uid, iso
from ..connectors.registry import registry

router = APIRouter(prefix="/api/v1")

STARTER_PACK = ["weather", "wikipedia", "jira", "slack", "notion", "github", "gmail", "helpdesk"]

STANDARD_ENTITY_RULES = [
    ("CREDIT_CARD", r"\b(?:\d[ -]?){12,19}\b", "REDACT"),
    ("EMAIL", r"[\w.+-]+@[\w-]+\.[\w.]+", "REDACT"),
    ("SECRET_TOKEN", r"\b(?:sk-|ghp_|xoxb-|cxk_|cxa_|cxg_|ntn_)[A-Za-z0-9_-]{10,}\b", "BLOCK"),
    ("PHONE", r"\+\d[\d -]{8,14}\d", "REDACT"),
    ("IP_ADDRESS", r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "REDACT"),
    ("DATE_OF_BIRTH", r"\b(?:19|20)\d\d[-/](?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])\b", "REDACT"),
]


def resolve_claim_page_url(request: Request, claim_token: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/claim/{claim_token}"


@router.post("/agent/signup/", status_code=201)
def agent_signup(request: Request, db: Session = Depends(get_db)):
    """Self-serve agent bootstrap. No credentials needed — this is the platform's designed
    agent lane. A human later claims the org via the claim link."""
    name = "agent-probe-org"
    org = Org(id=uid(), name=name, slug=f"{name}-{uid()[:8]}", kind="agent", claimed=False,
              claim_token=uid() + uid())
    db.add(org)
    db.flush()

    pack = ToolPack(id=uid(), agent_org_id=org.id, name="Starter Pack",
                    connectors=list(dict.fromkeys(STARTER_PACK)))
    db.add(pack)

    user = RegisteredUser(id=uid(), agent_org_id=org.id, origin_user_id=uid()[:10],
                          origin_user_name="Agent First User", origin_company_id=None)
    db.add(user)

    key = create_api_key(db, org=org, name="agent-default", scope="agent")

    for entity_type, pattern, action in STANDARD_ENTITY_RULES:
        db.add(SecurityRule(id=uid(), agent_org_id=org.id, entity_type=entity_type,
                            name=entity_type.replace("_", " ").title(), pattern=pattern,
                            action=action, scope="org", is_active=False))
    db.commit()

    audit(db, event_type="AGENT_SIGNUP", description=f"Agent org {org.slug} self-registered",
          agent_org_id=org.id, ip=request.client.host if request.client else None)
    return {
        "api_key": key.key_secret,
        "organization": {"id": org.id, "name": org.name, "slug": org.slug, "claimed": False},
        "tool_pack_id": pack.id,
        "registered_user_id": user.id,
        "claim_link": resolve_claim_page_url(request, org.claim_token or ""),
        "next_step": "Ask a human to open the claim link to take ownership of this organization.",
    }


def agent_key(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    key, org = require_scope("agent")(authorization=authorization, db=db)
    return key, org


# ---------------------------------------------------------------- catalog

@router.get("/connectors/")
def connectors(auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    catalog = []
    for connector in registry.list_connectors():
        tools = connector.tools
        catalog.append({
            "slug": connector.slug, "name": connector.name, "description": connector.description,
            "auth_type": connector.auth_type, "real": connector.real_lane_ready,
            "oauth_configured": bool(connector.oauth_ready),
            "tool_count": len(tools), "tools": [
                {"name": f"{connector.slug}__{t.name}", "description": t.description,
                 "credit_type": t.credit_type} for t in tools],
        })
    return {"count": len(catalog), "connectors": catalog}


@router.get("/connectors/{slug}/")
def connector_detail(slug: str, auth=Depends(agent_key), request: Request = None,
                     db: Session = Depends(get_db)):
    key, org = auth
    connector = registry.get(slug)
    if not connector:
        raise HTTPException(404, {"detail": f"Unknown connector '{slug}'."})
    return {
        "slug": connector.slug, "name": connector.name, "description": connector.description,
        "auth_type": connector.auth_type, "real": connector.real_lane_ready,
        "oauth_configured": bool(connector.oauth_ready),
        "tools": [{"name": f"{connector.slug}__{t.name}", "description": t.description,
                   "credit_type": t.credit_type, "annotations": t.annotations,
                   "schema": _outer_schema(t)} for t in connector.tools],
    }


def _outer_schema(t) -> dict:
    if t.input_model is not None:
        inner = t.input_model.model_json_schema()
        inner.setdefault("title", t.input_model.__name__)
        return {"type": "object", "required": ["input"],
                "properties": {"input": inner}, "additionalProperties": False}
    return {"type": "object", "required": ["input"],
            "properties": {"input": {"type": "object", "title": "Input",
                                     "additionalProperties": True}},
            "additionalProperties": False}

# ---------------------------------------------------------------- tool packs


@router.get("/tool-packs/")
def list_packs(auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    packs = db.query(ToolPack).filter(ToolPack.agent_org_id == org.id).all()
    return {"count": len(packs), "tool_packs": [
        {"id": p.id, "name": p.name, "connectors": p.connectors} for p in packs]}


@router.post("/tool-packs/", status_code=201)
def create_pack(data: dict, auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    connectors_list = data.get("connectors") or []
    unknown = [s for s in connectors_list if not registry.get(s)]
    if unknown:
        raise HTTPException(400, {"detail": [{"field": "connectors", "message": f"Unknown connectors: {unknown}"}]})
    pack = ToolPack(id=uid(), agent_org_id=org.id,
                    name=data.get("name") or "Custom Pack", connectors=connectors_list)
    db.add(pack)
    db.commit()
    audit(db, event_type="TOOL_PACK_CREATED", description=pack.name, agent_org_id=org.id)
    return {"id": pack.id, "name": pack.name, "connectors": pack.connectors}


@router.patch("/tool-packs/{pack_id}/connectors/")
def patch_pack_connectors(pack_id: str, data: dict, auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    pack = db.query(ToolPack).filter(ToolPack.id == pack_id, ToolPack.agent_org_id == org.id).first()
    if not pack:
        raise HTTPException(404, {"detail": "No ToolPack found."})
    connectors_in = data.get("connectors")
    if connectors_in is None:
        # tolerate a bare list body: ["slack"] or [{"connector": "slack"}]
        if isinstance(data, list):
            connectors_in = data
        else:
            raise HTTPException(400, {"connectors": ["This field is required."]})
    normalized: list[str] = []
    for item in connectors_in:
        slug = item.get("connector") if isinstance(item, dict) else item
        if not isinstance(slug, str):
            raise HTTPException(400, {"connectors": ["Expected a list of connector slugs or {\"connector\": slug} items."]})
        if not registry.get(slug):
            raise HTTPException(400, {"connectors": [f"Unknown connector '{slug}'."]})
        normalized.append(slug)
    pack.connectors = list(dict.fromkeys(normalized))
    db.commit()
    audit(db, event_type="TOOL_PACK_UPDATED", description=f"connectors -> {pack.connectors}", agent_org_id=org.id)
    return {"id": pack.id, "name": pack.name, "connectors": pack.connectors}

# ---------------------------------------------------------------- registered users


@router.get("/registered-users/")
def list_registered_users(auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    rows = db.query(RegisteredUser).filter(RegisteredUser.agent_org_id == org.id).all()
    return {"count": len(rows), "registered_users": [
        {"id": r.id, "origin_user_id": r.origin_user_id, "origin_user_name": r.origin_user_name,
         "origin_company_id": r.origin_company_id, "created_at": iso(r.created_at)} for r in rows]}


@router.post("/registered-users/", status_code=201)
def create_registered_user(data: dict, auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    errors: dict[str, list[str]] = {}
    if not data.get("origin_user_name"):
        errors["origin_user_name"] = ["This field is required."]
    if data.get("origin_user_id") is None:
        errors["origin_user_id"] = ["This field is required."]
    if errors:
        raise HTTPException(400, errors)
    user = RegisteredUser(id=uid(), agent_org_id=org.id,
                          origin_user_name=str(data["origin_user_name"]),
                          origin_user_id=str(data["origin_user_id"]),
                          origin_company_id=data.get("origin_company_id"))
    db.add(user)
    db.commit()
    audit(db, event_type="REGISTERED_USER_CREATED", description=user.origin_user_name,
          agent_org_id=org.id)
    return {"id": user.id, "origin_user_id": user.origin_user_id,
            "origin_user_name": user.origin_user_name,
            "origin_company_id": user.origin_company_id}

# ---------------------------------------------------------------- access keys


@router.get("/access-keys/")
def list_access_keys(auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    rows = db.query(ApiKey).filter(ApiKey.org_id == org.id, ApiKey.active == True).all()  # noqa: E712
    return {"count": len(rows), "access_keys": [
        {"id": r.id, "name": r.name, "key_masked": r.key_masked, "is_test": r.is_test,
         "created_at": iso(r.created_at)} for r in rows]}


class AccessKeyCreate(BaseModel):
    name: str = "untitled-key"
    tool_pack_id: str | None = None
    registered_user_ids: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)


@router.post("/access-keys/", status_code=201)
def create_access_key(data: AccessKeyCreate, auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    created = create_api_key(db, org=org, name=data.name, scope="agent")
    audit(db, event_type="PRODUCTION_API_KEY_CREATED",
          description=f"Created production access key: {created.name} ({created.id[:8]})",
          agent_org_id=org.id)
    return {"id": created.id, "name": created.name, "key": created.key_secret,
            "key_masked": created.key_masked, "tool_pack_ids": [data.tool_pack_id] if data.tool_pack_id else [],
            "registered_user_ids": data.registered_user_ids, "scopes": data.scopes,
            "is_test": created.is_test, "created_at": iso(created.created_at)}


@router.post("/access-keys/{key_id}/revoke/", status_code=204)
def revoke_access_key(key_id: str, auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    row = db.query(ApiKey).filter(ApiKey.id == key_id, ApiKey.org_id == org.id).first()
    if not row:
        raise HTTPException(404, {"detail": "No access key found."})
    row.active = False
    db.commit()
    audit(db, event_type="PRODUCTION_API_KEY_REVOKED", description=f"Production access key {row.name} revoked",
          agent_org_id=org.id)

# ---------------------------------------------------------------- security rules


@router.get("/security/rules/")
def list_security_rules(auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    rows = db.query(SecurityRule).filter(SecurityRule.agent_org_id == org.id).order_by(SecurityRule.entity_type).all()
    return {"count": len(rows), "rules": [
        {"id": r.id, "entity_type": r.entity_type, "name": r.name, "action": r.action,
         "scope": r.scope, "is_active": r.is_active} for r in rows]}


class SecurityRuleCreate(BaseModel):
    entity_type: str = Field(min_length=3, max_length=40)   # e.g. CUSTOM
    name: str
    pattern: str
    action: str = "REDACT"                                    # ALLOW | REDACT | BLOCK
    is_active: bool = True


@router.post("/security/rules/", status_code=201)
def create_security_rule(data: SecurityRuleCreate, auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    if data.action not in ("ALLOW", "REDACT", "BLOCK"):
        raise HTTPException(400, {"detail": "action must be ALLOW, REDACT or BLOCK."})
    try:
        re.compile(data.pattern)
    except re.error as error:
        raise HTTPException(400, {"detail": f"invalid regex: {error}"}) from error
    rule = SecurityRule(id=uid(), agent_org_id=org.id, entity_type=data.entity_type, name=data.name,
                        pattern=data.pattern, action=data.action, scope="org", is_active=data.is_active)
    db.add(rule)
    db.commit()
    audit(db, event_type="SECURITY_RULE_CREATED", description=f"{data.entity_type}/{data.action}",
          agent_org_id=org.id)
    return {"id": rule.id, "entity_type": rule.entity_type, "action": rule.action,
            "is_active": rule.is_active}


@router.patch("/security/rules/{rule_id}/")
def update_security_rule(rule_id: str, data: dict, auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    rule = db.query(SecurityRule).filter(SecurityRule.id == rule_id, SecurityRule.agent_org_id == org.id).first()
    if not rule:
        raise HTTPException(404, {"detail": "No rule found. Note: standard entity rules are governed — "
                                          "they may be activated but not have their pattern changed."})
    if "is_active" in data:
        rule.is_active = bool(data["is_active"])
    if "action" in data and rule.entity_type == "CUSTOM":
        if data["action"] not in ("ALLOW", "REDACT", "BLOCK"):
            raise HTTPException(400, {"detail": "action must be ALLOW, REDACT or BLOCK."})
        rule.action = data["action"]
    db.commit()
    return {"id": rule.id, "entity_type": rule.entity_type, "action": rule.action,
            "is_active": rule.is_active}

# ---------------------------------------------------------------- tool access requests


@router.post("/tool-access-requests/", status_code=201)
def create_tool_access_request(data: dict, auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    tool_name = data.get("tool_name")
    if not tool_name:
        raise HTTPException(400, {"tool_name": ["This field is required."]})
    row = ToolAccessRequest(id=uid(), agent_org_id=org.id,
                            tool_name=str(tool_name), reason=data.get("reason"),
                            requested_by=key.name)
    db.add(row)
    db.commit()
    audit(db, event_type="TOOL_ACCESS_REQUESTED", description=tool_name, agent_org_id=org.id)
    return {"id": row.id, "tool_name": row.tool_name, "status": row.status}


@router.get("/tool-access-requests/")
def list_tool_access_requests(auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    rows = db.query(ToolAccessRequest).filter(ToolAccessRequest.agent_org_id == org.id).order_by(
        ToolAccessRequest.created_at.desc()).all()
    return {"count": len(rows), "requests": [
        {"id": r.id, "tool_name": r.tool_name, "reason": r.reason, "status": r.status,
         "created_at": iso(r.created_at)} for r in rows]}

# ---------------------------------------------------------------- audit log


@router.get("/audit-log/")
def audit_log(auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    rows = (db.query(AuditEntry)
            .filter(AuditEntry.agent_org_id == org.id)
            .order_by(AuditEntry.ts.desc()).limit(50).all())
    return {"count": len(rows), "events": [
        {"event_type": r.event_type, "description": r.description, "actor": r.actor, "ts": iso(r.ts),
         "ip": r.ip} for r in rows]}


@router.get("/audit-log/export")
def audit_log_export(auth=Depends(agent_key), db: Session = Depends(get_db)):
    """Org-scoped audit trail as CSV (downloadable compliance artifact)."""
    key, org = auth
    rows = (db.query(AuditEntry)
            .filter(AuditEntry.agent_org_id == org.id)
            .order_by(AuditEntry.ts.desc()).limit(5000).all())
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["ts", "event_type", "actor", "ip", "description"])
    for row in rows:
        writer.writerow([iso(row.ts), row.event_type, row.actor or "", row.ip or "",
                         (row.description or "").replace("\n", " ")[:500]])
    audit(db, event_type="AUDIT_EXPORTED", org_id=None, agent_org_id=org.id,
          description=f"exported {len(rows)} audit rows as CSV")
    return Response(content=buffer.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="connexa-audit-log.csv"'})

# ---------------------------------------------------------------- stored credentials (paste-token lane)


class CredentialCreate(BaseModel):
    connector: str
    token: str = Field(min_length=8)
    refresh_token: str | None = None
    expires_in: int | None = None       # seconds until expiry (refresh lane)


@router.post("/registered-users/{registered_user_id}/credentials/", status_code=201)
def store_credentials(registered_user_id: str, data: CredentialCreate,
                      auth=Depends(agent_key), db: Session = Depends(get_db)):
    """Attach a real provider token to a registered user — the instant live lane.

    This makes the connector genuinely live without any OAuth app registration:
    paste the provider token (Slack bot token, Notion integration token, GitHub PAT)
    here and that user's tool calls hit the real provider from then on. An optional
    provider lookup annotates the row with a human account label (workspace/login).
    """
    key, org = auth
    connector = registry.get(data.connector)
    if not connector:
        raise HTTPException(400, {"connector": [f"Unknown connector '{data.connector}'."]})
    if connector.auth_type == "none":
        raise HTTPException(400, {"connector": [f"{connector.name} needs no credentials."]})
    user = db.query(RegisteredUser).filter(RegisteredUser.id == registered_user_id,
                                           RegisteredUser.agent_org_id == org.id).first()
    if not user:
        raise HTTPException(404, {"detail": "Unknown registered user for this agent org."})

    stored: dict = {"type": "user", "token": data.token}
    if data.refresh_token:
        stored["refresh_token"] = data.refresh_token
    if data.expires_in:
        from ..utils import naive_now
        stored["expires_at"] = (naive_now() + datetime.timedelta(seconds=int(data.expires_in))).isoformat()

    try:
        label = connector.describe_token(data.token)
    except Exception:  # noqa: BLE001
        label = None
    if label:
        stored["account_label"] = label

    user.credentials = {**(user.credentials or {}), data.connector: stored}
    db.commit()
    audit(db, event_type="AGENT_CREDENTIAL_PASTED",
          description=f"{connector.name} token attached to {user.origin_user_name}"
                      + (f" (verified: {label})" if label else " (unverified)"),
          actor=f"key:{key.name}", agent_org_id=org.id)
    return {
        "registered_user_id": user.id,
        "connector": data.connector,
        "status": "stored",
        "verified": bool(label),
        "account": label,
        "note": ("Token validated against the provider." if label else
                 "Token stored. The provider could not be reached to verify it — tool calls will "
                 "report the live error if the token is not accepted."),
    }
