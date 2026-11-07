"""Scheduled background sync.

One asyncio task (started on app boot) wakes every minute and syncs every
linked account whose per-account `sync_frequency_seconds` has elapsed — the
same `run_sync` engine the API and the console button use, so webhook events,
diff verdicts and audit entries are identical wherever a sync comes from.

Frequency is set per account over the API (`POST .../accounts/{token}/sync-frequency`)
or from the console. 0 disables scheduling for that account.
"""
from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy.orm import Session

from .metering import audit
from .models import LinkedAccount, SyncIssue
from .sync import run_sync
from .utils import naive_now, uid

POLL_INTERVAL_SECONDS = 60.0
_task_handle: asyncio.Task | None = None


def due_accounts(db: Session) -> list[LinkedAccount]:
    """Accounts with scheduling enabled whose interval has elapsed."""
    accounts = (db.query(LinkedAccount)
                .filter(LinkedAccount.sync_frequency_seconds > 0).all())
    now = naive_now()
    due: list[LinkedAccount] = []
    for account in accounts:
        elapsed = (now - account.last_synced_at).total_seconds() if account.last_synced_at else None
        if elapsed is None or elapsed >= account.sync_frequency_seconds:
            due.append(account)
    return due


def tick(db: Session, background=None) -> list[dict]:
    """One scheduler pass. Returns sync summaries; records SyncIssue rows on failure."""
    summaries: list[dict] = []
    for account in due_accounts(db):
        try:
            summary = run_sync(db, account, background=background, actor="scheduler")
            summaries.append(summary)
        except Exception as error:  # noqa: BLE001 — a bad connector must not kill the loop
            db.rollback()
            db.add(SyncIssue(id=uid(), org_id=account.org_id,
                             linked_account_id=account.id, model="sync", severity="error",
                             message=f"scheduled sync failed: {error}"))
            audit(db, event_type="SYNC_ISSUE", org_id=account.org_id, actor="scheduler",
                  description=f"{account.integration}: {str(error)[:120]}")
    return summaries


async def _loop() -> None:
    from .db import SessionLocal

    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            db = SessionLocal()
            try:
                tick(db)
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never die
            with contextlib.suppress(Exception):
                await asyncio.sleep(POLL_INTERVAL_SECONDS)


def start_scheduler() -> None:
    global _task_handle
    if _task_handle is None or _task_handle.done():
        _task_handle = asyncio.get_event_loop().create_task(_loop())


def stop_scheduler() -> None:
    global _task_handle
    if _task_handle is not None:
        _task_handle.cancel()
        _task_handle = None
