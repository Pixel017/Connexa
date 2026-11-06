"""Back-compat shim: the sync engine lives in .sync now.
`populate_linked_account` is the initial sync of a freshly linked account."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import LinkedAccount
from .sync import run_sync


def populate_linked_account(db: Session, account: LinkedAccount) -> int:
    summary = run_sync(db, account, background=None, actor="link")
    return summary["added"] + summary["updated"]
