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
from .models import LinkedAccount, Ticket, Contact, Deal, Org, ApiKey
from .common_models import COMMON_MODELS, TICKET_STATUSES, TICKET_PRIORITIES, DEAL_STAGES, CONTACT_STAGES
from .metering import usage, audit
from .utils import uid, now, iso
from .events import emit
from .connectors.registry import registry

router = APIRouter(prefix="/api")

# ------------------------------------------------------------ row mappers


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
    if category not in ("ticketing", "crm"):
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
    if category not in ("ticketing", "crm"):
        raise HTTPException(404, {"detail": f"Unknown category '{category}'."})
    account = (db.query(LinkedAccount)
               .filter(LinkedAccount.account_token == public_token, LinkedAccount.org_id == org.id)
               .first())
    if not account:
        raise HTTPException(404, {"detail": "Unknown public token."})
    if account.category != category:
        raise HTTPException(400, {"detail": f"This public token belongs to category '{account.category}'."})
    from .sync import run_sync
    summary = run_sync(db, account, background=background, actor="api")
    usage(db, plane="data", kind="sync_request", org_id=org.id, key_id=key.id, units=1.0, credits=0)
    return summary


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
def list_tickets(limit: int = 20, offset: int = 0, auth=Depends(data_plane_key),
                 db: Session = Depends(get_db), account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    rows = (db.query(Ticket).filter(Ticket.linked_account_id == account.id)
            .order_by(Ticket.created_at.desc()).limit(limit).offset(offset).all())
    usage(db, plane="data", kind="tickets_list", org_id=org.id, key_id=key.id, units=1.0, credits=0)
    return {"count": len(rows), "next": _next_cursor(limit, offset),
            "results": [_row_to_ticket(r) for r in rows]}


@router.get("/ticketing/v1/tickets/{ticket_id}")
def get_ticket(ticket_id: str, auth=Depends(data_plane_key), db: Session = Depends(get_db),
               account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    row = db.query(Ticket).filter(Ticket.id == ticket_id, Ticket.linked_account_id == account.id).first()
    if not row:
        raise HTTPException(404, {"detail": f"No Ticket found with id={ticket_id}."})
    return _row_to_ticket(row)


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
def list_contacts(limit: int = 20, offset: int = 0, auth=Depends(data_plane_key), db: Session = Depends(get_db),
                  account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    rows = (db.query(Contact).filter(Contact.linked_account_id == account.id)
            .order_by(Contact.created_at.desc()).limit(limit).offset(offset).all())
    return {"count": len(rows), "_next": _next_cursor(limit, offset),
            "results": [_row_to_contact(r) for r in rows]}


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
def list_deals(limit: int = 20, offset: int = 0, auth=Depends(data_plane_key), db: Session = Depends(get_db),
               account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    rows = (db.query(Deal).filter(Deal.linked_account_id == account.id)
            .order_by(Deal.created_at.desc()).limit(limit).offset(offset).all())
    return {"count": len(rows), "_next": _next_cursor(limit, offset),
            "results": [_row_to_deal(r) for r in rows]}


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
