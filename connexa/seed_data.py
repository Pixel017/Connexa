"""Populate a newly linked account with normalized rows from the provider's payload
(the real platform's initial sync, fed by simulated connectors).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import LinkedAccount, Ticket, Contact, Deal
from .utils import uid
from .connectors.registry import registry


def populate_linked_account(db: Session, account: LinkedAccount) -> int:
    connector = registry.require(account.integration)
    rows = connector.generate_sample(account.category)
    count = 0
    for row in rows:
        row_type = row.get("type") or ("ticket" if "title" in row else "contact")
        if account.category == "ticketing" and row_type != "ticket":
            continue
        if account.category == "crm" and row_type not in ("contact", "deal"):
            continue
        remote_extra = {"source": account.integration, "remote_payload": {
            k: v for k, v in row.items() if k not in ("type",)}}
        if account.category == "ticketing" and row_type == "ticket":
            db.add(Ticket(id=uid(), linked_account_id=account.id, remote_id=row.get("remote_id", "TCK-0"),
                          title=row.get("title", "Untitled"), status=row.get("status", "open"),
                          priority=row.get("priority", "medium"), assignee_name=row.get("assignee"),
                          description=row.get("description"), tags=row.get("tags", []),
                          remote_extra=remote_extra))
            count += 1
        elif account.category == "crm" and row_type == "contact":
            db.add(Contact(id=uid(), linked_account_id=account.id, remote_id=row.get("remote_id", "CT-0"),
                           first_name=row.get("first_name"), last_name=row.get("last_name"),
                           email=row.get("email"), phone=row.get("phone"), company=row.get("company"),
                           stage=row.get("stage"), owner_name=row.get("owner_name"),
                           remote_extra=remote_extra))
            count += 1
        elif account.category == "crm" and row_type == "deal":
            db.add(Deal(id=uid(), linked_account_id=account.id, remote_id=row.get("remote_id", "DL-0"),
                        name=row.get("name", "Untitled deal"), stage=row.get("stage", "prospecting"),
                        amount=row.get("amount"), currency=row.get("currency", "USD"),
                        owner_name=row.get("owner_name"), contact_remote_id=row.get("contact_remote_id"),
                        remote_extra=remote_extra))
            count += 1
    db.commit()
    return count
