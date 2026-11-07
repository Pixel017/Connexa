"""Workforce plane — govern the AI an entire company uses.

Four capabilities, all enforced in-path (not cosmetic):

* IdP/SCIM 2.0 provisioning — /scim/v2/Users + /scim/v2/Groups map directory
  users and groups onto registered users and workforce teams. Membership in a
  team is what grants tool packs, model allowlists and budgets.
* Per-team model allowlists — the gateway refuses models outside a team's
  allowlist (and filters failover chains to allowed models only).
* Per-team budgets — a hard monthly credit cap per team, checked before any
  model invocation; 402 when exhausted.
* Shared skills library + employee AI context — versioned skills and org/team
  scoped memories, exposed both over REST and as MCP tools for agents.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .db import get_db
from .models import (Budget, RegisteredUser, Skill, SyncIssue,
                     TeamMembership, ToolPack, UsageLog, WorkforceMemory,
                     WorkforceTeam, Org)
from .metering import audit, used_credits_this_month
from .utils import now, uid, iso

router = APIRouter(prefix="/api/workforce/v1", tags=["workforce"])
scim_router = APIRouter(prefix="/scim/v2", tags=["scim"])


def workforce_key(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    """Workforce APIs accept the org's data/admin keys (the company's own key)."""
    from .security import resolve_key

    key, org = resolve_key(db, authorization)
    if key.scope not in ("data", "admin"):
        raise HTTPException(403, {"detail": "This endpoint requires a data or admin key."})
    return key, org


def _team_dict(db: Session, team: WorkforceTeam) -> dict:
    members = (db.query(TeamMembership)
               .filter(TeamMembership.team_id == team.id).all())
    pack = db.query(ToolPack).filter(ToolPack.id == team.tool_pack_id).first() if team.tool_pack_id else None
    budget = db.query(Budget).filter(Budget.team_id == team.id).first()
    used = round(used_team_credits_this_month(db, team.id), 4)
    return {"id": team.id, "name": team.name, "idp_external_id": team.idp_external_id,
            "tool_pack_id": team.tool_pack_id, "tool_pack": pack.name if pack else None,
            "allowed_models": team.allowed_models or ["*"],
            "monthly_credit_budget": team.monthly_credit_budget,
            "credits_used_this_month": used,
            "members": [{"user_id": m.registered_user_id, "role": m.role} for m in members],
            "created_at": iso(team.created_at)}


def used_team_credits_this_month(db: Session, team_id: str) -> float:
    start = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = (db.query(UsageLog)
            .filter(UsageLog.plane == "gateway", UsageLog.team_id == team_id, UsageLog.ts >= start)
            .all())
    return sum(row.credits for row in rows)


def resolve_team(db: Session, org: Org, reference: str) -> WorkforceTeam | None:
    """Teams are referenced by id or name (X-Connexa-Team accepts either)."""
    return (db.query(WorkforceTeam)
            .filter(WorkforceTeam.org_id == org.id,
                    (WorkforceTeam.id == reference) | (WorkforceTeam.name == reference))
            .first())


def enforce_team_model(db: Session, org: Org, team_reference: str | None, model_slug: str | None):
    """Gateway hook: resolve the team, enforce its model allowlist and budget.

    Returns the team (or None when no team header was presented)."""
    if not team_reference:
        return None
    team = resolve_team(db, org, team_reference)
    if not team:
        raise HTTPException(404, {"error": {"code": "unknown_team",
                                            "message": f"No workforce team '{team_reference}' in this org."}})
    allowed = team.allowed_models or ["*"]
    if model_slug and "*" not in allowed and model_slug not in allowed:
        audit(db, event_type="TEAM_MODEL_BLOCKED", org_id=org.id,
              description=f"{team.name}: {model_slug} not in team allowlist", actor="gateway")
        raise HTTPException(403, {"error": {"code": "model_not_allowed_for_team",
                                            "message": (f"Model '{model_slug}' is not approved for team "
                                                        f"'{team.name}'. Approved: {allowed}")}})
    if team.monthly_credit_budget is not None:
        used = used_team_credits_this_month(db, team.id)
        if used >= team.monthly_credit_budget:
            audit(db, event_type="TEAM_BUDGET_CUTOFF", org_id=org.id,
                  description=f"{team.name}: {used:g}/{team.monthly_credit_budget} cr", actor="gateway")
            raise HTTPException(402, {"error": {"code": "team_budget_exceeded",
                                                "message": (f"Monthly gateway budget exhausted for team "
                                                            f"'{team.name}' "
                                                            f"({used:g}/{team.monthly_credit_budget} cr).")}})
    return team


def dlp_scan_outbound(db: Session, org: Org, text: str) -> tuple[str, list[dict], bool]:
    """DLP on outbound model traffic — same rule engine as agent tool traffic."""
    from .agent.mcp import apply_gateway

    return apply_gateway(org.id, text, db)


# ---------------------------------------------------------------- teams API

class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    tool_pack_id: str | None = None
    allowed_models: list[str] = Field(default_factory=lambda: ["*"])
    monthly_credit_budget: int | None = None


@router.post("/teams", status_code=201)
def create_team(data: TeamCreate, auth=Depends(workforce_key), db: Session = Depends(get_db)):
    key, org = auth
    if data.tool_pack_id:
        pack = db.query(ToolPack).filter(ToolPack.id == data.tool_pack_id,
                                         ToolPack.agent_org_id == org.id).first()
        if not pack:
            raise HTTPException(404, {"detail": f"No tool pack {data.tool_pack_id} in this org."})
    team = WorkforceTeam(id=uid(), org_id=org.id, name=data.name.strip(),
                         tool_pack_id=data.tool_pack_id,
                         allowed_models=data.allowed_models or ["*"],
                         monthly_credit_budget=data.monthly_credit_budget)
    db.add(team)
    db.commit()
    audit(db, event_type="TEAM_CREATED", description=team.name, org_id=org.id, actor=f"key:{key.name}")
    return _team_dict(db, team)


@router.get("/teams")
def list_teams(auth=Depends(workforce_key), db: Session = Depends(get_db)):
    _key, org = auth
    teams = db.query(WorkforceTeam).filter(WorkforceTeam.org_id == org.id).all()
    return {"count": len(teams), "teams": [_team_dict(db, t) for t in teams]}


@router.patch("/teams/{team_id}")
def patch_team(team_id: str, data: dict, auth=Depends(workforce_key), db: Session = Depends(get_db)):
    key, org = auth
    team = db.query(WorkforceTeam).filter(WorkforceTeam.id == team_id, WorkforceTeam.org_id == org.id).first()
    if not team:
        raise HTTPException(404, {"detail": "Unknown team for this org."})
    if "name" in data and str(data["name"]).strip():
        team.name = str(data["name"]).strip()
    if "tool_pack_id" in data:
        team.tool_pack_id = data["tool_pack_id"] or None
    if "allowed_models" in data and isinstance(data["allowed_models"], list):
        team.allowed_models = data["allowed_models"]
    if "monthly_credit_budget" in data:
        team.monthly_credit_budget = data["monthly_credit_budget"]
    db.commit()
    audit(db, event_type="TEAM_UPDATED", description=team.name, org_id=org.id, actor=f"key:{key.name}")
    return _team_dict(db, team)


@router.post("/teams/{team_id}/members", status_code=201)
def add_member(team_id: str, data: dict, auth=Depends(workforce_key), db: Session = Depends(get_db)):
    key, org = auth
    team = db.query(WorkforceTeam).filter(WorkforceTeam.id == team_id, WorkforceTeam.org_id == org.id).first()
    if not team:
        raise HTTPException(404, {"detail": "Unknown team for this org."})
    user_ref = str(data.get("registered_user_id") or data.get("origin_user_id") or "")
    user = (db.query(RegisteredUser)
            .filter(RegisteredUser.agent_org_id == org.id,
                    (RegisteredUser.id == user_ref) | (RegisteredUser.origin_user_id == user_ref))
            .first())
    if not user:
        raise HTTPException(404, {"detail": f"No registered user '{user_ref}' in this org."})
    existing = (db.query(TeamMembership)
                .filter(TeamMembership.team_id == team.id,
                        TeamMembership.registered_user_id == user.id).first())
    if existing:
        return {"team_id": team.id, "user_id": user.id, "role": existing.role, "already_member": True}
    membership = TeamMembership(id=uid(), team_id=team.id, registered_user_id=user.id,
                                role=str(data.get("role") or "member"))
    db.add(membership)
    db.commit()
    audit(db, event_type="TEAM_MEMBER_ADDED", description=f"{team.name} <- {user.origin_user_name}",
          org_id=org.id, actor=f"key:{key.name}")
    return {"team_id": team.id, "user_id": user.id, "user": user.origin_user_name, "role": membership.role}


@router.delete("/teams/{team_id}/members/{user_id}", status_code=200)
def remove_member(team_id: str, user_id: str, auth=Depends(workforce_key), db: Session = Depends(get_db)):
    _key, org = auth
    membership = (db.query(TeamMembership).join(WorkforceTeam, WorkforceTeam.id == TeamMembership.team_id)
                  .filter(TeamMembership.team_id == team_id, TeamMembership.registered_user_id == user_id,
                          WorkforceTeam.org_id == org.id).first())
    if not membership:
        raise HTTPException(404, {"detail": "No such membership."})
    db.delete(membership)
    db.commit()
    audit(db, event_type="TEAM_MEMBER_REMOVED", description=f"{team_id} - {user_id}", org_id=org.id)
    return {"removed": True}


# ---------------------------------------------------------------- skills library

class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    instructions: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    team_id: str | None = None     # None = shared org-wide


@router.get("/skills")
def list_skills(team_id: str | None = None, auth=Depends(workforce_key), db: Session = Depends(get_db)):
    _key, org = auth
    query = db.query(Skill).filter(Skill.org_id == org.id)
    if team_id:
        query = query.filter((Skill.team_id == team_id) | (Skill.team_id == None))  # noqa: E711
    rows = query.order_by(Skill.name).all()
    return {"count": len(rows), "skills": [
        {"id": s.id, "name": s.name, "instructions": s.instructions, "tags": s.tags,
         "team_id": s.team_id, "version": s.version, "shared": s.team_id is None,
         "updated_at": iso(s.updated_at)} for s in rows]}


@router.post("/skills", status_code=201)
def create_skill(data: SkillCreate, auth=Depends(workforce_key), db: Session = Depends(get_db)):
    key, org = auth
    if data.team_id:
        team = db.query(WorkforceTeam).filter(WorkforceTeam.id == data.team_id, WorkforceTeam.org_id == org.id).first()
        if not team:
            raise HTTPException(404, {"detail": "Unknown team for this org."})
    skill = Skill(id=uid(), org_id=org.id, team_id=data.team_id, name=data.name.strip(),
                  instructions=data.instructions, tags=data.tags)
    db.add(skill)
    db.commit()
    audit(db, event_type="SKILL_PUBLISHED", description=skill.name, org_id=org.id, actor=f"key:{key.name}")
    return {"id": skill.id, "name": skill.name, "team_id": skill.team_id, "version": skill.version}


@router.patch("/skills/{skill_id}")
def patch_skill(skill_id: str, data: SkillCreate, auth=Depends(workforce_key), db: Session = Depends(get_db)):
    key, org = auth
    skill = db.query(Skill).filter(Skill.id == skill_id, Skill.org_id == org.id).first()
    if not skill:
        raise HTTPException(404, {"detail": "Unknown skill."})
    skill.name = data.name.strip()
    skill.instructions = data.instructions
    skill.tags = data.tags
    if data.team_id is not None:
        skill.team_id = data.team_id
    skill.version += 1
    skill.updated_at = now()
    db.commit()
    audit(db, event_type="SKILL_UPDATED", description=f"{skill.name} v{skill.version}",
          org_id=org.id, actor=f"key:{key.name}")
    return {"id": skill.id, "name": skill.name, "version": skill.version}


# ---------------------------------------------------------------- employee AI context

class MemoryCreate(BaseModel):
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    team_id: str | None = None
    author: str | None = None


@router.get("/memories")
def list_memories(q: str | None = None, team_id: str | None = None, limit: int = 20,
                  auth=Depends(workforce_key), db: Session = Depends(get_db)):
    _key, org = auth
    query = db.query(WorkforceMemory).filter(WorkforceMemory.org_id == org.id)
    if team_id:
        query = query.filter((WorkforceMemory.team_id == team_id) | (WorkforceMemory.team_id == None))  # noqa: E711
    if q:
        like = f"%{q}%"
        query = query.filter(WorkforceMemory.content.like(like))
    rows = query.order_by(WorkforceMemory.created_at.desc()).limit(min(limit, 100)).all()
    return {"count": len(rows), "memories": [
        {"id": m.id, "content": m.content, "author": m.author, "tags": m.tags,
         "team_id": m.team_id, "created_at": iso(m.created_at)} for m in rows]}


@router.post("/memories", status_code=201)
def create_memory(data: MemoryCreate, auth=Depends(workforce_key), db: Session = Depends(get_db)):
    key, org = auth
    memory = WorkforceMemory(id=uid(), org_id=org.id, team_id=data.team_id,
                             author=data.author or f"key:{key.name}", content=data.content, tags=data.tags)
    db.add(memory)
    db.commit()
    audit(db, event_type="MEMORY_SAVED", description=memory.content[:80], org_id=org.id, actor=f"key:{key.name}")
    return {"id": memory.id, "content": memory.content, "author": memory.author, "tags": memory.tags}


# ---------------------------------------------------------------- SCIM 2.0

SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"


def scim_key(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    """IdPs authenticate with a bearer key (admin or data scoped)."""
    from .security import resolve_key

    key, org = resolve_key(db, authorization)
    if key.scope not in ("data", "admin"):
        raise HTTPException(403, {"detail": "SCIM requires a data or admin key."})
    return key, org


def _scim_user_payload(user: RegisteredUser) -> dict:
    return {"schemas": [SCIM_USER_SCHEMA], "id": user.id,
            "externalId": user.idp_external_id,
            "userName": user.origin_user_id,
            "name": {"formatted": user.origin_user_name},
            "displayName": user.origin_user_name,
            "active": bool(user.active),
            "meta": {"resourceType": "User"}}


@scim_router.get("/Users")
def scim_list_users(auth=Depends(scim_key), db: Session = Depends(get_db)):
    _key, org = auth
    users = db.query(RegisteredUser).filter(RegisteredUser.agent_org_id == org.id).all()
    return {"schemas": [SCIM_LIST_SCHEMA], "totalResults": len(users),
            "Resources": [_scim_user_payload(u) for u in users]}


@scim_router.post("/Users", status_code=201)
def scim_create_user(data: dict, auth=Depends(scim_key), db: Session = Depends(get_db)):
    """Provision a user from the IdP. Idempotent on userName / externalId."""
    key, org = auth
    user_name = str(data.get("userName") or "").strip()
    if not user_name:
        raise HTTPException(400, {"detail": "SCIM User requires userName."})
    existing = (db.query(RegisteredUser)
                .filter(RegisteredUser.agent_org_id == org.id,
                        (RegisteredUser.origin_user_id == user_name) |
                        (RegisteredUser.idp_external_id == str(data.get("externalId") or "") and bool(data.get("externalId"))))
                .first())
    if existing:
        return _scim_user_payload(existing)
    display = ((data.get("name") or {}).get("formatted")) or data.get("displayName") or user_name
    user = RegisteredUser(id=uid(), agent_org_id=org.id, origin_user_id=user_name,
                          origin_user_name=str(display), origin_company_id=org.id,
                          idp_external_id=data.get("externalId"), active=bool(data.get("active", True)))
    db.add(user)
    db.commit()
    audit(db, event_type="SCIM_USER_PROVISIONED", description=user_name, org_id=org.id, actor=f"key:{key.name}")
    return _scim_user_payload(user)


@scim_router.get("/Users/{user_id}")
def scim_get_user(user_id: str, auth=Depends(scim_key), db: Session = Depends(get_db)):
    _key, org = auth
    user = db.query(RegisteredUser).filter(RegisteredUser.id == user_id,
                                           RegisteredUser.agent_org_id == org.id).first()
    if not user:
        raise HTTPException(404, {"detail": "User not found."})
    return _scim_user_payload(user)


@scim_router.patch("/Users/{user_id}")
def scim_patch_user(user_id: str, data: dict, auth=Depends(scim_key), db: Session = Depends(get_db)):
    """IdP-driven activation/deactivation (deprovisioning)."""
    _key, org = auth
    user = db.query(RegisteredUser).filter(RegisteredUser.id == user_id,
                                           RegisteredUser.agent_org_id == org.id).first()
    if not user:
        raise HTTPException(404, {"detail": "User not found."})
    for op in data.get("Operations") or []:
        if op.get("op") == "replace" and isinstance(op.get("value"), dict) and "active" in op["value"]:
            user.active = bool(op["value"]["active"])
    db.commit()
    audit(db, event_type="SCIM_USER_PATCHED", description=f"{user.origin_user_id}: active={user.active}",
          org_id=org.id)
    return _scim_user_payload(user)


def _scim_group_payload(db: Session, team: WorkforceTeam) -> dict:
    members = db.query(TeamMembership).filter(TeamMembership.team_id == team.id).all()
    return {"schemas": [SCIM_GROUP_SCHEMA], "id": team.id,
            "externalId": team.idp_external_id, "displayName": team.name,
            "members": [{"value": m.registered_user_id, "display": m.registered_user_id}
                        for m in members],
            "meta": {"resourceType": "Group"}}


@scim_router.get("/Groups")
def scim_list_groups(auth=Depends(scim_key), db: Session = Depends(get_db)):
    _key, org = auth
    teams = db.query(WorkforceTeam).filter(WorkforceTeam.org_id == org.id).all()
    return {"schemas": [SCIM_LIST_SCHEMA], "totalResults": len(teams),
            "Resources": [_scim_group_payload(db, t) for t in teams]}


@scim_router.post("/Groups", status_code=201)
def scim_create_group(data: dict, auth=Depends(scim_key), db: Session = Depends(get_db)):
    """IdP group -> workforce team. Membership grants the team's pack/allowlist/budget.

    Optionally pass connexa_extensions: {tool_pack_id, allowed_models, monthly_credit_budget}."""
    key, org = auth
    name = str(data.get("displayName") or "").strip()
    if not name:
        raise HTTPException(400, {"detail": "SCIM Group requires displayName."})
    team = (db.query(WorkforceTeam).filter(WorkforceTeam.org_id == org.id,
                                           (WorkforceTeam.name == name) |
                                           (WorkforceTeam.idp_external_id == str(data.get("externalId") or "") and bool(data.get("externalId"))))
            .first())
    if not team:
        extensions = data.get("connexa_extensions") or {}
        team = WorkforceTeam(id=uid(), org_id=org.id, name=name,
                             idp_external_id=data.get("externalId"),
                             tool_pack_id=extensions.get("tool_pack_id"),
                             allowed_models=extensions.get("allowed_models") or ["*"],
                             monthly_credit_budget=extensions.get("monthly_credit_budget"))
        db.add(team)
    members = data.get("members") or []
    for member in members:
        ref = str(member.get("value") or "")
        user = (db.query(RegisteredUser)
                .filter(RegisteredUser.agent_org_id == org.id,
                        (RegisteredUser.id == ref) | (RegisteredUser.idp_external_id == ref))
                .first())
        if user and not db.query(TeamMembership).filter(
                TeamMembership.team_id == team.id, TeamMembership.registered_user_id == user.id).first():
            db.add(TeamMembership(id=uid(), team_id=team.id, registered_user_id=user.id))
    db.commit()
    audit(db, event_type="SCIM_GROUP_SYNCED", description=f"{name}: {len(members)} members",
          org_id=org.id, actor=f"key:{key.name}")
    return _scim_group_payload(db, team)


@scim_router.patch("/Groups/{team_id}")
def scim_patch_group(team_id: str, data: dict, auth=Depends(scim_key), db: Session = Depends(get_db)):
    """IdP membership deltas: add/remove members, rename."""
    _key, org = auth
    team = db.query(WorkforceTeam).filter(WorkforceTeam.id == team_id, WorkforceTeam.org_id == org.id).first()
    if not team:
        raise HTTPException(404, {"detail": "Group not found."})
    for op in data.get("Operations") or []:
        op_name = str(op.get("op") or "").lower()
        value = op.get("value")
        if op_name == "replace" and isinstance(value, dict) and value.get("displayName"):
            team.name = str(value["displayName"])
        elif op_name in ("add", "replace") and isinstance(value, dict) and "members" in (value or {}):
            for member in value["members"] or []:
                ref = str(member.get("value") or "")
                user = (db.query(RegisteredUser)
                        .filter(RegisteredUser.agent_org_id == org.id,
                                (RegisteredUser.id == ref) | (RegisteredUser.idp_external_id == ref)).first())
                if user and not db.query(TeamMembership).filter(
                        TeamMembership.team_id == team.id, TeamMembership.registered_user_id == user.id).first():
                    db.add(TeamMembership(id=uid(), team_id=team.id, registered_user_id=user.id))
        elif op_name == "remove":
            path = str(op.get("path") or "")
            if path.startswith("members"):
                ref = str((value or {}).get("value") or op.get("value") or "")
                membership = (db.query(TeamMembership)
                              .filter(TeamMembership.team_id == team.id,
                                      TeamMembership.registered_user_id == ref).first())
                if membership:
                    db.delete(membership)
    db.commit()
    audit(db, event_type="SCIM_GROUP_SYNCED", description=f"{team.name} patched", org_id=org.id)
    return _scim_group_payload(db, team)


# ---------------------------------------------------------------- issues + health

@router.get("/issues")
def workforce_issues(auth=Depends(workforce_key), db: Session = Depends(get_db)):
    _key, org = auth
    rows = (db.query(SyncIssue).filter(SyncIssue.org_id == org.id, SyncIssue.resolved == False)  # noqa: E712
            .order_by(SyncIssue.created_at.desc()).limit(50).all())
    return {"count": len(rows), "issues": [
        {"id": i.id, "model": i.model, "severity": i.severity, "message": i.message,
         "created_at": iso(i.created_at)} for i in rows]}
