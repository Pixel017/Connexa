"""Provider -> Common Model sync engine.

Pulls a linked account's provider payload through its connector and upserts it
into the unified tables by remote_id — the same operation runs on link (initial
sync), from the data-plane API, and from the dashboard "Sync now" button.
Every run reports exactly what moved: added / updated / unchanged, so callers
and webhooks can show (or react to) the diff.
"""
from __future__ import annotations

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from .connectors.registry import registry
from .events import emit
from .metering import audit, usage
from .models import Contact, Deal, LinkedAccount, Ticket
from .utils import iso, now, uid


def _remote_extra(account: LinkedAccount, row: dict) -> dict:
    return {"source": account.integration, "remote_payload": {k: v for k, v in row.items() if k != "type"}}


def _apply(existing, data: dict) -> bool:
    """Write changed fields only. Returns True if anything actually differed."""
    changed = False
    for name, value in data.items():
        if getattr(existing, name, None) != value:
            setattr(existing, name, value)
            changed = True
    return changed


def _upsert_ticket(db: Session, account: LinkedAccount, row: dict) -> str:
    remote_id = str(row.get("remote_id") or "TCK-0")
    remote_extra = _remote_extra(account, row)
    data = dict(
        title=row.get("title") or "Untitled",
        status=row.get("status") or "open",
        priority=row.get("priority") or "medium",
        assignee_name=row.get("assignee"),
        description=row.get("description"),
        tags=row.get("tags") or [],
    )
    existing = (db.query(Ticket)
                .filter(Ticket.linked_account_id == account.id, Ticket.remote_id == remote_id)
                .one_or_none())
    if existing is None:
        db.add(Ticket(id=uid(), linked_account_id=account.id, remote_id=remote_id,
                      remote_extra=remote_extra, **data))
        return "added"
    changed = _apply(existing, data)
    existing.remote_extra = remote_extra   # raw payload always reflects the latest pull
    return "updated" if changed else "unchanged"


def _upsert_contact(db: Session, account: LinkedAccount, row: dict) -> str:
    remote_id = str(row.get("remote_id") or "CT-0")
    remote_extra = _remote_extra(account, row)
    data = dict(
        first_name=row.get("first_name"),
        last_name=row.get("last_name"),
        email=row.get("email"),
        phone=row.get("phone"),
        company=row.get("company"),
        stage=row.get("stage"),
        owner_name=row.get("owner_name"),
    )
    existing = (db.query(Contact)
                .filter(Contact.linked_account_id == account.id, Contact.remote_id == remote_id)
                .one_or_none())
    if existing is None:
        db.add(Contact(id=uid(), linked_account_id=account.id, remote_id=remote_id,
                       remote_extra=remote_extra, **data))
        return "added"
    changed = _apply(existing, data)
    existing.remote_extra = remote_extra
    return "updated" if changed else "unchanged"


def _upsert_deal(db: Session, account: LinkedAccount, row: dict) -> str:
    remote_id = str(row.get("remote_id") or "DL-0")
    remote_extra = _remote_extra(account, row)
    data = dict(
        name=row.get("name") or "Untitled deal",
        stage=row.get("stage") or "prospecting",
        amount=row.get("amount"),
        currency=row.get("currency") or "USD",
        owner_name=row.get("owner_name"),
        contact_remote_id=row.get("contact_remote_id"),
    )
    existing = (db.query(Deal)
                .filter(Deal.linked_account_id == account.id, Deal.remote_id == remote_id)
                .one_or_none())
    if existing is None:
        db.add(Deal(id=uid(), linked_account_id=account.id, remote_id=remote_id,
                    remote_extra=remote_extra, **data))
        return "added"
    changed = _apply(existing, data)
    existing.remote_extra = remote_extra
    return "updated" if changed else "unchanged"


_LOADER = {"ticket": _upsert_ticket, "contact": _upsert_contact, "deal": _upsert_deal}


def run_sync(db: Session, account: LinkedAccount, background: BackgroundTasks | None = None,
             actor: str = "api") -> dict:
    """Sync one linked account. Idempotent: re-running adds nothing new.

    Returns {"integration", "category", "added", "updated", "unchanged",
             "counts": {model: n}, "synced_at"} for API and dashboard alike.
    """
    connector = registry.require(account.integration)
    rows = connector.generate_sample(account.category) or []
    wanted = ("ticket",) if account.category == "ticketing" else ("contact", "deal")

    outcome = {"added": 0, "updated": 0, "unchanged": 0}
    counts: dict[str, int] = {}
    synced_models: list[str] = []
    for row in rows:
        row_type = row.get("type") or ("ticket" if "title" in row else "contact")
        if row_type not in wanted:
            continue
        loader = _LOADER[row_type]
        verdict = loader(db, account, row)
        outcome[verdict] += 1
        model = {"ticket": "Ticket", "contact": "Contact", "deal": "Deal"}[row_type]
        counts[model] = counts.get(model, 0) + 1
        synced_models.append(model)

    account.last_synced_at = now()
    db.commit()

    usage(db, plane="data", kind="sync", org_id=account.org_id, units=float(len(rows)), credits=0,
          meta={"integration": account.integration, **counts})
    audit(db, event_type="ACCOUNT_SYNCED",
          description=(f"{account.category}/{account.integration}: "
                       f"+{outcome['added']} ~{outcome['updated']} ={outcome['unchanged']}"),
          org_id=account.org_id, actor=actor)

    if background is not None and (outcome["added"] or outcome["updated"]):
        emit(db, background, event="LinkedAccount.synced", org_id=account.org_id, payload_data={
            "linked_account_id": account.id, "integration": account.integration,
            "category": account.category, "added": outcome["added"],
            "updated": outcome["updated"], "counts": counts})

    return {"integration": account.integration, "category": account.category,
            "added": outcome["added"], "updated": outcome["updated"],
            "unchanged": outcome["unchanged"], "counts": counts,
            "synced_at": iso(now())}
