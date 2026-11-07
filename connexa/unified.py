"""Unified data plane — one Common Model per category, backed by linked provider accounts.

Auth model:
    Authorization: Bearer <data API key>
    X-Account-Token: <per-linked-account token>

Patterns implemented (mirroring the platform we reverse-engineered):
  * cursor'd list endpoints, single get, create, update
  * /meta/write-schema discovery endpoint before writes
  * raw-payload embed ("remote_data") on every row
  * passthrough escape hatch to the provider's own API
  * account-token exchange endpoint per category
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .db import get_db
from .security import data_plane_key
from .models import (LinkedAccount, SyncIssue, Ticket, Contact, Deal, Employee, Invoice, Org, ApiKey)
from .common_models import (COMMON_MODELS, DATA_CATEGORIES, TICKET_STATUSES, TICKET_PRIORITIES,
                            DEAL_STAGES, CONTACT_STAGES)
from .metering import usage, audit
from .utils import uid, now, iso
from .events import emit
from .connectors.registry import registry

router = APIRouter(prefix="/api")

# ------------------------------------------------------------ row mappers


def _apply_field_scopes(key, model: str, payload: dict) -> dict:
    """Field-level scopes on the presenting key: {"Ticket": {"exclude": [...]}} or
    {"Ticket": {"include": [...]}} — data minimization enforced at the response layer."""
    scopes = (getattr(key, "field_scopes", None) or {})
    rule = scopes.get(model) if isinstance(scopes, dict) else None
    if not rule:
        return payload
    if isinstance(rule.get("include"), list):
        allowed = set(rule["include"])
        return {name: value for name, value in payload.items() if name in allowed}
    if isinstance(rule.get("exclude"), list):
        blocked = set(rule["exclude"])
        return {name: value for name, value in payload.items() if name not in blocked}
    return payload


def _minimize(payload: dict, include_remote_data: bool) -> dict:
    if include_remote_data:
        return payload
    return {name: value for name, value in payload.items() if name != "remote_data"}


def _row_to_ticket(t: Ticket) -> dict:
    return {"id": t.id, "remote_id": t.remote_id, "title": t.title, "status": t.status,
            "priority": t.priority, "assignee": t.assignee_name, "description": t.description,
            "tags": t.tags, "created_at": iso(t.created_at), "updated_at": iso(t.updated_at),
            "remote_data": t.remote_extra}


def _row_to_contact(c: Contact) -> dict:
    return {"id": c.id, "remote_id": c.remote_id, "first_name": c.first_name, "last_name": c.last_name,
            "email": c.email, "phone": c.phone, "company": c.company, "stage": c.stage,
            "owner": c.owner_name, "created_at": iso(c.created_at), "updated_at": iso(c.updated_at),
            "remote_data": c.remote_extra}


def _row_to_deal(d: Deal) -> dict:
    return {"id": d.id, "remote_id": d.remote_id, "name": d.name, "stage": d.stage,
            "amount": d.amount, "currency": d.currency, "owner": d.owner_name,
            "created_at": iso(d.created_at), "updated_at": iso(d.updated_at), "remote_data": d.remote_extra}


def _account(authorization_required_account: str | None, db: Session, org: Org) -> LinkedAccount:
    token = authorization_required_account
    if not token:
        raise HTTPException(401, {"detail": "X-Account-Token header required."})
    account = (
        db.query(LinkedAccount)
        .filter(LinkedAccount.account_token == token, LinkedAccount.org_id == org.id,
                LinkedAccount.status == "live")
        .first()
    )
    if not account:
        raise HTTPException(404, {"detail": "Unknown account token for this organization."})
    return account


def _resolve_public_token(category: str, public_token: str, org: Org, db: Session) -> LinkedAccount:
    if category not in DATA_CATEGORIES:
        raise HTTPException(404, {"detail": f"Unknown category '{category}'."})
    account = (db.query(LinkedAccount)
               .filter(LinkedAccount.account_token == public_token, LinkedAccount.org_id == org.id)
               .first())
    if not account:
        raise HTTPException(404, {"detail": "Unknown public token."})
    if account.category != category:
        raise HTTPException(400, {"detail": f"This public token belongs to category '{account.category}'."})
    return account


def _pager(rows: list, request_path: str, limit_param: int, offset_param: int) -> dict:
    page = rows[offset_param: offset_param + limit_param]
    next_offset = offset_param + limit_param if offset_param + limit_param < len(rows) else None
    next_cursor = f"{request_path}?limit={limit_param}&offset={next_offset}" if next_offset is not None else None
    return {
        "results": page,
        "count": len(rows),
        "_next": next_cursor,
    }

# ------------------------------------------------------------ account-token exchange

@router.post("/{category}/v1/account-token/{public_token}")
def exchange_account_token(category: str, public_token: str,
                           background: BackgroundTasks,
                           auth=Depends(data_plane_key), db: Session = Depends(get_db)):
    if category not in DATA_CATEGORIES:
        raise HTTPException(404, {"detail": f"Unknown category '{category}'."})
    key, org = auth
    account = db.query(LinkedAccount).filter(
        LinkedAccount.account_token == public_token,
        LinkedAccount.org_id == org.id).first()
    if not account:
        raise HTTPException(404, {"detail": "Unknown public token."})
    if account.category != category:
        raise HTTPException(400, {"detail": f"This public token belongs to category '{account.category}'."})
    audit(db, event_type="ACCOUNT_TOKEN_EXCHANGED",
          description=f"Exchange for {account.integration} linked account", org_id=org.id, key_id=key.id)
    return {"account_token": account.account_token,
            "account": {"id": account.id, "category": account.category,
                        "integration": account.integration,
                        "linked_at": iso(account.linked_at)}}

# ------------------------------------------------------------ linked accounts view

@router.get("/linked-accounts")
def list_linked_accounts(auth=Depends(data_plane_key), db: Session = Depends(get_db)):
    key, org = auth
    rows = db.query(LinkedAccount).filter(LinkedAccount.org_id == org.id).order_by(LinkedAccount.linked_at).all()
    return {"count": len(rows), "linked_accounts": [
        {"id": r.id, "category": r.category, "integration": r.integration,
         "status": r.status, "end_user_name": r.end_user_name,
         "account_token_masked": r.account_token[:9] + "…",
         "linked_at": iso(r.linked_at)} for r in rows]}

# ------------------------------------------------------------ on-demand sync

@router.post("/{category}/v1/accounts/{public_token}/sync")
def sync_account(category: str, public_token: str, background: BackgroundTasks,
                 auth=Depends(data_plane_key), db: Session = Depends(get_db)):
    key, org = auth
    account = _resolve_public_token(category, public_token, org, db)
    from .sync import run_sync
    summary = run_sync(db, account, background=background, actor="api")
    usage(db, plane="data", kind="sync_request", org_id=org.id, key_id=key.id, units=1.0, credits=0)
    return summary


class SyncFrequency(BaseModel):
    seconds: int = Field(ge=0, le=86_400 * 7)   # 0 = manual only


@router.post("/{category}/v1/accounts/{public_token}/sync-frequency")
def set_sync_frequency(category: str, public_token: str, data: SyncFrequency,
                       auth=Depends(data_plane_key), db: Session = Depends(get_db)):
    """Scheduled background sync: the platform re-pulls this account automatically."""
    key, org = auth
    account = _resolve_public_token(category, public_token, org, db)
    account.sync_frequency_seconds = data.seconds
    db.commit()
    audit(db, event_type="SYNC_SCHEDULE_SET", org_id=org.id, key_id=key.id,
          description=f"{account.integration}: every {data.seconds}s" if data.seconds
          else f"{account.integration}: manual only")
    return {"integration": account.integration, "sync_frequency_seconds": account.sync_frequency_seconds,
            "scheduled": account.sync_frequency_seconds > 0}


@router.get("/{category}/v1/issues")
def list_issues(category: str, auth=Depends(data_plane_key), db: Session = Depends(get_db),
                status: str = "open"):
    """Sync health for this org's accounts (Merge-style issues surface)."""
    _key, org = auth
    query = db.query(SyncIssue).filter(SyncIssue.org_id == org.id)
    query = query.filter(SyncIssue.resolved == False) if status == "open" else query  # noqa: E712
    rows = query.order_by(SyncIssue.created_at.desc()).limit(100).all()
    return {"count": len(rows), "status": status, "issues": [
        {"id": issue.id, "linked_account_id": issue.linked_account_id, "model": issue.model,
         "severity": issue.severity, "message": issue.message,
         "created_at": iso(issue.created_at)} for issue in rows]}


@router.delete("/{category}/v1/accounts/{public_token}")
def erase_account(category: str, public_token: str, auth=Depends(data_plane_key),
                  db: Session = Depends(get_db)):
    """Access & erasure: remove a linked account and every normalized row derived
    from it. Required for end-user deletion requests (GDPR-style erasure)."""
    key, org = auth
    if category not in DATA_CATEGORIES:
        raise HTTPException(404, {"detail": f"Unknown category '{category}'."})
    account = db.query(LinkedAccount).filter(
        LinkedAccount.account_token == public_token, LinkedAccount.org_id == org.id).first()
    if not account:
        raise HTTPException(404, {"detail": "Unknown public token."})
    if account.category != category:
        raise HTTPException(400, {"detail": f"This public token belongs to category '{account.category}'."})
    counts = {"Ticket": 0, "Contact": 0, "Deal": 0, "Employee": 0, "Invoice": 0}
    counts["Ticket"] = db.query(Ticket).filter(Ticket.linked_account_id == account.id).delete()
    counts["Contact"] = db.query(Contact).filter(Contact.linked_account_id == account.id).delete()
    counts["Deal"] = db.query(Deal).filter(Deal.linked_account_id == account.id).delete()
    counts["Employee"] = db.query(Employee).filter(Employee.linked_account_id == account.id).delete()
    counts["Invoice"] = db.query(Invoice).filter(Invoice.linked_account_id == account.id).delete()
    db.query(SyncIssue).filter(SyncIssue.linked_account_id == account.id).delete()
    integration, erased_id = account.integration, account.id
    db.delete(account)
    db.commit()
    audit(db, event_type="ERASURE_REQUEST_COMPLETED", org_id=org.id, key_id=key.id,
          description=f"{integration}: account {erased_id} + {counts} rows removed")
    return {"erased": True, "integration": integration, "rows_deleted": counts}


# ------------------------------------------------------------ ticketing

class TicketCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    status: str = "open"
    priority: str = "medium"
    assignee: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class TicketUpdate(BaseModel):
    status: str | None = None
    priority: str | None = None
    assignee: str | None = None
    tags: list[str] | None = None
    description: str | None = None


@router.get("/ticketing/v1/tickets")
def list_tickets(limit: int = 20, offset: int = 0, include_remote_data: bool = True,
                 auth=Depends(data_plane_key),
                 db: Session = Depends(get_db), account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    rows = (db.query(Ticket).filter(Ticket.linked_account_id == account.id)
            .order_by(Ticket.created_at.desc()).limit(limit).offset(offset).all())
    usage(db, plane="data", kind="tickets_list", org_id=org.id, key_id=key.id, units=1.0, credits=0)
    return {"count": len(rows), "next": _next_cursor(limit, offset),
            "results": [_minimize(_apply_field_scopes(key, "Ticket", _row_to_ticket(r)),
                                  include_remote_data) for r in rows]}


@router.get("/ticketing/v1/tickets/{ticket_id}")
def get_ticket(ticket_id: str, include_remote_data: bool = True, auth=Depends(data_plane_key),
               db: Session = Depends(get_db),
               account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    row = db.query(Ticket).filter(Ticket.id == ticket_id, Ticket.linked_account_id == account.id).first()
    if not row:
        raise HTTPException(404, {"detail": f"No Ticket found with id={ticket_id}."})
    return _minimize(_apply_field_scopes(key, "Ticket", _row_to_ticket(row)), include_remote_data)


@router.post("/ticketing/v1/tickets", status_code=201)
def create_ticket(data: TicketCreate, background: BackgroundTasks, auth=Depends(data_plane_key),
                  db: Session = Depends(get_db),
                  account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    if data.status not in TICKET_STATUSES:
        raise HTTPException(400, {"detail": [{"field": "status", "message": f"'{data.status}' is not a valid choice.",
                                              "allowed": TICKET_STATUSES}]})
    if data.priority not in TICKET_PRIORITIES:
        raise HTTPException(400, {"detail": [{"field": "priority", "message": f"'{data.priority}' is not a valid choice.",
                                              "allowed": TICKET_PRIORITIES}]})
    row = Ticket(id=uid(), linked_account_id=account.id, remote_id=f"TCK-{_next_remote(account.id)}",
                 title=data.title, status=data.status, priority=data.priority,
                 assignee_name=data.assignee, description=data.description, tags=data.tags,
                 remote_extra={"source": account.integration, "written": True})
    db.add(row)
    account.last_synced_at = now()
    db.commit()
    emit(db, background, event="Ticket.synced", org_id=org.id, payload_data={
        "ticket_id": row.id, "remote_id": row.remote_id, "title": row.title,
        "integration": account.integration})
    usage(db, plane="data", kind="ticket_write", org_id=org.id, key_id=key.id,
          units=1.0, credit_type="medium")
    audit(db, event_type="TICKET_CREATED", description=row.remote_id, org_id=org.id, key_id=key.id)
    return _row_to_ticket(row)


@router.patch("/ticketing/v1/tickets/{ticket_id}")
def patch_ticket(ticket_id: str, data: TicketUpdate, background: BackgroundTasks,
                 auth=Depends(data_plane_key), db: Session = Depends(get_db),
                 account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    row = db.query(Ticket).filter(Ticket.id == ticket_id, Ticket.linked_account_id == account.id).first()
    if not row:
        raise HTTPException(404, {"detail": f"No Ticket found with id={ticket_id}."})
    if data.status and data.status not in TICKET_STATUSES:
        raise HTTPException(400, {"detail": [{"field": "status", "message": f"'{data.status}' is not a valid choice.",
                                              "allowed": TICKET_STATUSES}]})
    if data.priority and data.priority not in TICKET_PRIORITIES:
        raise HTTPException(400, {"detail": [{"field": "priority", "message": f"'{data.priority}' is not a valid choice.",
                                              "allowed": TICKET_PRIORITIES}]})
    for name, value in (("status", data.status), ("priority", data.priority),
                        ("assignee_name", data.assignee), ("description", data.description),
                        ("tags", data.tags)):
        if value is not None:
            setattr(row, name, value)
    row.updated_at = now()
    db.commit()
    emit(db, background, event="Ticket.synced", org_id=org.id, payload_data={
        "ticket_id": row.id, "remote_id": row.remote_id, "title": row.title,
        "integration": account.integration})
    usage(db, plane="data", kind="ticket_write", org_id=org.id, key_id=key.id, units=1.0, credit_type="low")
    return _row_to_ticket(row)


@router.get("/ticketing/v1/meta/write-schema")
def ticketing_meta(auth=Depends(data_plane_key), db: Session = Depends(get_db)):
    key, org = auth
    payload = COMMON_MODELS["ticketing"]["Ticket"]
    usage(db, plane="data", kind="meta_disc", org_id=org.id, key_id=key.id, units=1.0, credits=0)
    return {"model": "Ticket", "category": "ticketing", "fields": payload["fields"],
            "read_only_fields": payload["read_only"]}


@router.post("/ticketing/v1/passthrough")
def ticketing_passthrough(data: dict, auth=Depends(data_plane_key), db: Session = Depends(get_db),
                          account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    connector = registry.require(account.integration)
    ctx = _connector_ctx(account)
    method = str(data.get("method", "GET")).upper()
    path = str(data.get("path", "/myself"))
    raw = connector.passthrough(ctx, method, path)
    usage(db, plane="data", kind="passthrough", org_id=org.id, key_id=key.id, units=1.0, credit_type="medium")
    return raw

# ------------------------------------------------------------ crm

class ContactCreate(BaseModel):
    first_name: str = Field(min_length=1)
    last_name: str | None = None
    email: str | None = None
    company: str | None = None
    stage: str | None = None


class DealCreate(BaseModel):
    name: str = Field(min_length=1)
    stage: str = "prospecting"
    amount: float | None = None
    currency: str | None = None


@router.get("/crm/v1/contacts")
def list_contacts(limit: int = 20, offset: int = 0, include_remote_data: bool = True,
                  auth=Depends(data_plane_key), db: Session = Depends(get_db),
                  account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    rows = (db.query(Contact).filter(Contact.linked_account_id == account.id)
            .order_by(Contact.created_at.desc()).limit(limit).offset(offset).all())
    return {"count": len(rows), "_next": _next_cursor(limit, offset),
            "results": [_minimize(_apply_field_scopes(key, "Contact", _row_to_contact(r)),
                                  include_remote_data) for r in rows]}


@router.post("/crm/v1/contacts", status_code=201)
def create_contact(data: ContactCreate, background: BackgroundTasks, auth=Depends(data_plane_key),
                   db: Session = Depends(get_db),
                   account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    if data.stage and data.stage not in CONTACT_STAGES:
        raise HTTPException(400, {"detail": [{"field": "stage", "message": f"'{data.stage}' is not a valid choice.",
                                              "allowed": CONTACT_STAGES}]})
    row = Contact(id=uid(), linked_account_id=account.id, remote_id=f"CT-{_next_remote(account.id)}",
                  first_name=data.first_name, last_name=data.last_name, email=data.email,
                  company=data.company, stage=data.stage or "lead",
                  remote_extra={"source": account.integration, "written": True})
    db.add(row)
    db.commit()
    emit(db, background, event="Contact.synced", org_id=org.id, payload_data={
        "contact_id": row.id, "remote_id": row.remote_id, "email": row.email,
        "integration": account.integration})
    usage(db, plane="data", kind="crm_write", org_id=org.id, key_id=key.id, units=1.0, credit_type="medium")
    return _row_to_contact(row)


@router.get("/crm/v1/deals")
def list_deals(limit: int = 20, offset: int = 0, include_remote_data: bool = True,
               auth=Depends(data_plane_key), db: Session = Depends(get_db),
               account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    rows = (db.query(Deal).filter(Deal.linked_account_id == account.id)
            .order_by(Deal.created_at.desc()).limit(limit).offset(offset).all())
    return {"count": len(rows), "_next": _next_cursor(limit, offset),
            "results": [_minimize(_apply_field_scopes(key, "Deal", _row_to_deal(r)),
                                  include_remote_data) for r in rows]}


@router.post("/crm/v1/deals", status_code=201)
def create_deal(data: DealCreate, background: BackgroundTasks, auth=Depends(data_plane_key),
                db: Session = Depends(get_db),
                account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    if data.stage not in DEAL_STAGES:
        raise HTTPException(400, {"detail": [{"field": "stage", "message": f"'{data.stage}' is not a valid choice.",
                                              "allowed": DEAL_STAGES}]})
    row = Deal(id=uid(), linked_account_id=account.id, remote_id=f"DL-{_next_remote(account.id)}",
               name=data.name, stage=data.stage, amount=data.amount,
               currency=data.currency or "USD", remote_extra={"source": account.integration, "written": True})
    db.add(row)
    db.commit()
    emit(db, background, event="Deal.synced", org_id=org.id, payload_data={
        "deal_id": row.id, "remote_id": row.remote_id, "name": row.name,
        "integration": account.integration})
    usage(db, plane="data", kind="crm_write", org_id=org.id, key_id=key.id, units=1.0, credit_type="medium")
    return _row_to_deal(row)


@router.get("/crm/v1/meta/write-schema")
def crm_meta(auth=Depends(data_plane_key), db: Session = Depends(get_db)):
    key, org = auth
    usage(db, plane="data", kind="meta_disc", org_id=org.id, key_id=key.id, units=1.0, credits=0)
    return {"category": "crm",
            "Contact": COMMON_MODELS["crm"]["Contact"],
            "Deal": COMMON_MODELS["crm"]["Deal"]}

# ------------------------------------------------------------ helpers

def _next_cursor(limit: int, offset: int) -> str | None:
    return f"limit={limit}&offset={offset + limit}" if limit else None


_REMOTE_COUNTER: dict[str, int] = {}


def _next_remote(linked_account_id: str) -> int:
    _REMOTE_COUNTER[linked_account_id] = _REMOTE_COUNTER.get(linked_account_id, 9000) + 1
    return _REMOTE_COUNTER[linked_account_id]


def _connector_ctx(account: LinkedAccount):
    from .connectors.registry import registry
    from .connectors.base import ToolContext
    connector = registry.require(account.integration)
    return ToolContext(connector_slug=connector.slug, credentials=account.credentials,
                       env_token=connector.real_lane_ready and getattr(connector, "env_token", "") or "",
                       registered_user_id=None, agent_org_id=None)


# ------------------------------------------------------------ HRIS

def _row_to_employee(e: Employee) -> dict:
    return {"id": e.id, "remote_id": e.remote_id, "first_name": e.first_name, "last_name": e.last_name,
            "email": e.email, "department": e.department, "position": e.position,
            "manager_name": e.manager_name, "employment_type": e.employment_type,
            "created_at": iso(e.created_at), "updated_at": iso(e.updated_at),
            "remote_data": e.remote_extra}


class EmployeeCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = ""
    email: str | None = None
    department: str | None = None
    position: str | None = None
    manager_name: str | None = None
    employment_type: str = "full_time"


@router.get("/hris/v1/employees")
def list_employees(limit: int = 20, offset: int = 0, include_remote_data: bool = True,
                   auth=Depends(data_plane_key), db: Session = Depends(get_db),
                   account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    rows = (db.query(Employee).filter(Employee.linked_account_id == account.id)
            .order_by(Employee.created_at.desc()).limit(limit).offset(offset).all())
    return {"count": len(rows), "_next": _next_cursor(limit, offset),
            "results": [_minimize(_apply_field_scopes(key, "Employee", _row_to_employee(r)),
                                  include_remote_data) for r in rows]}


@router.get("/hris/v1/employees/{employee_id}")
def get_employee(employee_id: str, include_remote_data: bool = True, auth=Depends(data_plane_key),
                 db: Session = Depends(get_db),
                 account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    row = db.query(Employee).filter(Employee.id == employee_id, Employee.linked_account_id == account.id).first()
    if not row:
        raise HTTPException(404, {"detail": f"No Employee found with id={employee_id}."})
    return _minimize(_apply_field_scopes(key, "Employee", _row_to_employee(row)), include_remote_data)


@router.post("/hris/v1/employees", status_code=201)
def create_employee(data: EmployeeCreate, background: BackgroundTasks, auth=Depends(data_plane_key),
                    db: Session = Depends(get_db),
                    account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    if data.employment_type not in COMMON_MODELS["hris"]["Employee"]["fields"]["employment_type"]["values"]:
        raise HTTPException(400, {"detail": f"employment_type must be one of "
                                            f"{COMMON_MODELS['hris']['Employee']['fields']['employment_type']['values']}."})
    row = Employee(id=uid(), linked_account_id=account.id, remote_id=str(_next_remote(account.id)),
                   first_name=data.first_name, last_name=data.last_name, email=data.email,
                   department=data.department, position=data.position, manager_name=data.manager_name,
                   employment_type=data.employment_type,
                   remote_extra={"source": account.integration, "written": True})
    db.add(row)
    db.commit()
    emit(db, background, event="Employee.synced", org_id=org.id, payload_data={
        "employee_id": row.id, "remote_id": row.remote_id, "integration": account.integration})
    usage(db, plane="data", kind="hris_write", org_id=org.id, key_id=key.id, units=1.0, credit_type="low")
    return _row_to_employee(row)


@router.get("/hris/v1/meta/write-schema")
def hris_meta(auth=Depends(data_plane_key), db: Session = Depends(get_db)):
    _key, _org = auth
    return {"category": "hris", "Employee": COMMON_MODELS["hris"]["Employee"]}


# ------------------------------------------------------------ accounting

def _row_to_invoice(inv: Invoice) -> dict:
    return {"id": inv.id, "remote_id": inv.remote_id, "invoice_number": inv.invoice_number,
            "status": inv.status, "vendor_name": inv.vendor_name, "total_amount": inv.total_amount,
            "currency": inv.currency, "issue_date": inv.issue_date, "due_date": inv.due_date,
            "memo": inv.memo, "created_at": iso(inv.created_at), "updated_at": iso(inv.updated_at),
            "remote_data": inv.remote_extra}


class InvoiceCreate(BaseModel):
    vendor_name: str = Field(min_length=1, max_length=160)
    total_amount: float = Field(gt=0)
    invoice_number: str | None = None
    status: str = "draft"
    currency: str = "USD"
    issue_date: str | None = None
    due_date: str | None = None
    memo: str | None = None


@router.get("/accounting/v1/invoices")
def list_invoices(limit: int = 20, offset: int = 0, include_remote_data: bool = True,
                  auth=Depends(data_plane_key), db: Session = Depends(get_db),
                  account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    rows = (db.query(Invoice).filter(Invoice.linked_account_id == account.id)
            .order_by(Invoice.created_at.desc()).limit(limit).offset(offset).all())
    return {"count": len(rows), "_next": _next_cursor(limit, offset),
            "results": [_minimize(_apply_field_scopes(key, "Invoice", _row_to_invoice(r)),
                                  include_remote_data) for r in rows]}


@router.get("/accounting/v1/invoices/{invoice_id}")
def get_invoice(invoice_id: str, include_remote_data: bool = True, auth=Depends(data_plane_key),
                db: Session = Depends(get_db),
                account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    row = db.query(Invoice).filter(Invoice.id == invoice_id, Invoice.linked_account_id == account.id).first()
    if not row:
        raise HTTPException(404, {"detail": f"No Invoice found with id={invoice_id}."})
    return _minimize(_apply_field_scopes(key, "Invoice", _row_to_invoice(row)), include_remote_data)


@router.post("/accounting/v1/invoices", status_code=201)
def create_invoice(data: InvoiceCreate, background: BackgroundTasks, auth=Depends(data_plane_key),
                   db: Session = Depends(get_db),
                   account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    if data.status not in COMMON_MODELS["accounting"]["Invoice"]["fields"]["status"]["values"]:
        raise HTTPException(400, {"detail": f"status must be one of "
                                            f"{COMMON_MODELS['accounting']['Invoice']['fields']['status']['values']}."})
    row = Invoice(id=uid(), linked_account_id=account.id, remote_id=str(_next_remote(account.id)),
                  invoice_number=data.invoice_number, status=data.status, vendor_name=data.vendor_name,
                  total_amount=data.total_amount, currency=data.currency or "USD",
                  issue_date=data.issue_date, due_date=data.due_date, memo=data.memo,
                  remote_extra={"source": account.integration, "written": True})
    db.add(row)
    db.commit()
    emit(db, background, event="Invoice.synced", org_id=org.id, payload_data={
        "invoice_id": row.id, "remote_id": row.remote_id, "integration": account.integration})
    usage(db, plane="data", kind="accounting_write", org_id=org.id, key_id=key.id, units=1.0, credit_type="medium")
    return _row_to_invoice(row)


@router.get("/accounting/v1/meta/write-schema")
def accounting_meta(auth=Depends(data_plane_key), db: Session = Depends(get_db)):
    _key, _org = auth
    return {"category": "accounting", "Invoice": COMMON_MODELS["accounting"]["Invoice"]}


# ------------------------------------------------------------ field mappings

class FieldMappingsUpdate(BaseModel):
    """{model: {remote_field: common_field}} — replaces the mappings for the listed models."""
    mappings: dict[str, dict[str, str]]


@router.get("/{category}/v1/accounts/{public_token}/field-mappings")
def get_field_mappings(category: str, public_token: str, auth=Depends(data_plane_key),
                       db: Session = Depends(get_db)):
    _key, org = auth
    account = _resolve_public_token(category, public_token, org, db)
    return {"integration": account.integration,
            "mappings": (account.settings or {}).get("field_mappings") or {},
            "common_models": {m: sorted(COMMON_MODELS[category][m]["fields"])
                              for m in COMMON_MODELS[category]["models"]}}


@router.put("/{category}/v1/accounts/{public_token}/field-mappings")
def put_field_mappings(category: str, public_token: str, data: FieldMappingsUpdate,
                       auth=Depends(data_plane_key), db: Session = Depends(get_db)):
    """Set per-account field mappings. Every target must be a real Common Model field —
    the sync engine applies them to provider payloads before normalization."""
    key, org = auth
    account = _resolve_public_token(category, public_token, org, db)
    for model, pairs in data.mappings.items():
        if model not in COMMON_MODELS[category]["models"]:
            raise HTTPException(400, {"detail": f"Unknown model '{model}' for category '{category}'."})
        valid = set(COMMON_MODELS[category][model]["fields"])
        for remote_field, common_field in pairs.items():
            if common_field not in valid:
                raise HTTPException(400, {"detail": f"'{common_field}' is not a field of {model} "
                                                    f"(valid: {sorted(valid)})."})
    settings = dict(account.settings or {})
    merged = {**settings.get("field_mappings", {}), **data.mappings}
    settings["field_mappings"] = merged
    account.settings = settings
    db.commit()
    audit(db, event_type="FIELD_MAPPINGS_SET", org_id=org.id, key_id=key.id,
          description=f"{account.integration}: {merged}")
    # Re-sync immediately so mappings take effect on existing remote payloads.
    from .sync import run_sync
    summary = run_sync(db, account, actor="field-mappings")
    return {"integration": account.integration, "mappings": merged,
            "reapplied_sync": {"added": summary["added"], "updated": summary["updated"],
                               "unchanged": summary["unchanged"]}}


@router.get("/{category}/v1/accounts/{public_token}/field-mappings/suggestions")
def field_mapping_suggestions(category: str, public_token: str, auth=Depends(data_plane_key),
                              db: Session = Depends(get_db)):
    """Provider fields seen in recent payloads that don't map onto a Common Model field,
    with a best-effort target suggestion (name similarity + value-shape sniffing)."""
    _key, org = auth
    account = _resolve_public_token(category, public_token, org, db)
    settings = account.settings or {}
    mappings = settings.get("field_mappings") or {}
    common_fields = {m: set(COMMON_MODELS[category][m]["fields"]) for m in COMMON_MODELS[category]["models"]}

    def _suggest(target: str, candidates: list[str]) -> str | None:
        target_key = target.replace("_", "")
        for candidate in candidates:
            if candidate.replace("_", "") == target_key:
                return candidate
        return None

    from .models import Ticket as _T, Contact as _C, Deal as _D, Employee as _E, Invoice as _I
    row_models = {"Ticket": _T, "Contact": _C, "Deal": _D, "Employee": _E, "Invoice": _I}
    suggestions: dict[str, dict] = {}
    for model, fields in common_fields.items():
        table = row_models.get(model)
        if table is None:
            continue
        seen: dict[str, int] = {}
        for extra in (db.query(table.remote_extra)
                      .filter(table.linked_account_id == account.id).limit(50).all()):
            payload = (extra[0] or {}).get("remote_payload") or {}
            for field_name in payload:
                seen[field_name] = seen.get(field_name, 0) + 1
        mapped = mappings.get(model) or {}
        unmapped = {name: count for name, count in seen.items()
                    if name not in fields and name not in mapped}
        if unmapped:
            proposals = {name: _suggest(name, sorted(fields)) for name in unmapped}
            suggestions[model] = {"unmapped_provider_fields": unmapped,
                                  "suggested_targets": proposals}
    return {"integration": account.integration, "suggestions": suggestions}
