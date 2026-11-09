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

from .connectors.base import RealPullError
from .connectors.registry import registry
from .events import emit
from .metering import audit, usage
from .models import (Candidate, Contact, Deal, Employee, FileRow, Folder, Invoice,
                     LinkedAccount, SyncIssue, Ticket)
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


def _upsert_employee(db: Session, account: LinkedAccount, row: dict) -> str:
    remote_id = str(row.get("remote_id") or "EMP-0")
    remote_extra = _remote_extra(account, row)
    data = dict(
        first_name=row.get("first_name"),
        last_name=row.get("last_name"),
        email=row.get("email"),
        department=row.get("department"),
        position=row.get("position"),
        manager_name=row.get("manager_name") or row.get("manager"),
        employment_type=row.get("employment_type"),
    )
    existing = (db.query(Employee)
                .filter(Employee.linked_account_id == account.id, Employee.remote_id == remote_id)
                .one_or_none())
    if existing is None:
        db.add(Employee(id=uid(), linked_account_id=account.id, remote_id=remote_id,
                        remote_extra=remote_extra, **data))
        return "added"
    changed = _apply(existing, data)
    existing.remote_extra = remote_extra
    return "updated" if changed else "unchanged"


def _upsert_invoice(db: Session, account: LinkedAccount, row: dict) -> str:
    remote_id = str(row.get("remote_id") or "INV-0")
    remote_extra = _remote_extra(account, row)
    data = dict(
        invoice_number=row.get("invoice_number"),
        status=row.get("status") or "draft",
        vendor_name=row.get("vendor_name") or row.get("vendor"),
        total_amount=row.get("total_amount") if row.get("total_amount") is not None else row.get("total"),
        currency=row.get("currency") or "USD",
        issue_date=row.get("issue_date") or row.get("issued"),
        due_date=row.get("due_date") or row.get("due"),
        memo=row.get("memo"),
    )
    existing = (db.query(Invoice)
                .filter(Invoice.linked_account_id == account.id, Invoice.remote_id == remote_id)
                .one_or_none())
    if existing is None:
        db.add(Invoice(id=uid(), linked_account_id=account.id, remote_id=remote_id,
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


def _upsert_candidate(db: Session, account: LinkedAccount, row: dict) -> str:
    """ATS: provider candidate payload -> normalized Candidate row."""
    remote_id = str(row.get("remote_id") or "CAN-0")
    remote_extra = _remote_extra(account, row)
    data = dict(
        first_name=row.get("first_name"),
        last_name=row.get("last_name"),
        email=row.get("email"),
        current_company=row.get("current_company"),
        role=row.get("role"),
        location=row.get("location"),
        application_status=row.get("application_status") or "applied",
        recruiter_name=row.get("recruiter"),
        notes=row.get("notes"),
        tags=row.get("tags") or [],
    )
    existing = (db.query(Candidate)
                .filter(Candidate.linked_account_id == account.id, Candidate.remote_id == remote_id)
                .one_or_none())
    if existing is None:
        db.add(Candidate(id=uid(), linked_account_id=account.id, remote_id=remote_id,
                         remote_extra=remote_extra, **data))
        return "added"
    changed = _apply(existing, data)
    existing.remote_extra = remote_extra
    return "updated" if changed else "unchanged"


def _upsert_file(db: Session, account: LinkedAccount, row: dict) -> str:
    """File Storage: provider file payload -> normalized File row."""
    remote_id = str(row.get("remote_id") or "FILE-0")
    remote_extra = _remote_extra(account, row)
    data = dict(
        name=row.get("name") or "Untitled",
        mime_type=row.get("mime_type") or row.get("content_type"),
        size_bytes=row.get("size_bytes") if row.get("size_bytes") is not None else row.get("size"),
        folder=row.get("folder") or row.get("path"),
        uploaded_by=row.get("uploaded_by") or row.get("owner"),
        tags=row.get("tags") or [],
    )
    existing = (db.query(FileRow)
                .filter(FileRow.linked_account_id == account.id, FileRow.remote_id == remote_id)
                .one_or_none())
    if existing is None:
        db.add(FileRow(id=uid(), linked_account_id=account.id, remote_id=remote_id,
                       remote_extra=remote_extra, **data))
        return "added"
    changed = _apply(existing, data)
    existing.remote_extra = remote_extra
    return "updated" if changed else "unchanged"


def _upsert_folder(db: Session, account: LinkedAccount, row: dict) -> str:
    """File Storage: provider folder payload -> normalized Folder row."""
    remote_id = str(row.get("remote_id") or "FOLDER-0")
    remote_extra = _remote_extra(account, row)
    data = dict(
        name=row.get("name") or "Untitled",
        parent=row.get("parent") or row.get("path"),
    )
    existing = (db.query(Folder)
                .filter(Folder.linked_account_id == account.id, Folder.remote_id == remote_id)
                .one_or_none())
    if existing is None:
        db.add(Folder(id=uid(), linked_account_id=account.id, remote_id=remote_id,
                      remote_extra=remote_extra, **data))
        return "added"
    changed = _apply(existing, data)
    existing.remote_extra = remote_extra
    return "updated" if changed else "unchanged"


_LOADER = {"ticket": _upsert_ticket, "contact": _upsert_contact, "deal": _upsert_deal,
           "employee": _upsert_employee, "invoice": _upsert_invoice,
           "candidate": _upsert_candidate, "file": _upsert_file, "folder": _upsert_folder}

_MODEL_NAMES = {"ticket": "Ticket", "contact": "Contact", "deal": "Deal",
                "employee": "Employee", "invoice": "Invoice",
                "candidate": "Candidate", "file": "File", "folder": "Folder"}

_WANTED = {"ticketing": ("ticket",), "crm": ("contact", "deal"),
           "hris": ("employee",), "accounting": ("invoice",),
           "ats": ("candidate",), "filestorage": ("folder", "file")}


def _apply_field_mappings(account: LinkedAccount, model: str, row: dict) -> dict:
    """Per-account field mappings (Merge-style): {remote_field: common_field}.

    Provider payloads use the provider's own names; mappings rename them onto the
    Common Model before upsert. Unmapped provider fields keep flowing into
    remote_data (nothing is dropped).
    """
    mappings = (account.settings or {}).get("field_mappings") or {}
    for_model = mappings.get(model) or {}
    for remote_field, common_field in for_model.items():
        if remote_field in row and common_field and common_field not in row:
            row = {**row, common_field: row[remote_field]}
    return row


def run_sync(db: Session, account: LinkedAccount, background: BackgroundTasks | None = None,
             actor: str = "api") -> dict:
    """Sync one linked account. Idempotent: re-running adds nothing new.

    Honors per-account governance held in account.settings:
      * selective_sync:      {"Ticket": false, ...} — models the customer excluded
      * ignored_remote_ids:  remote objects to never sync (access/erasure tooling)

    Every per-row failure is recorded as a SyncIssue (surfaced via /issues and
    the console) instead of aborting the whole pull.

    Returns {"integration", "category", "added", "updated", "unchanged",
             "counts": {model: n}, "skipped", "errors", "synced_at"}.
    """
    connector = registry.require(account.integration)
    if (connector.real_lane_ready and getattr(connector, "data_categories", None)
            and account.category in connector.data_categories):
        # REAL data lane: pull live provider data with the account's credentials.
        try:
            rows = connector.pull_remote(account.category, account.credentials or {}) or []
        except RealPullError as error:
            # No rollback here: the caller may have uncommitted state it must keep
            # (connect's pending LinkedAccount insert). Record the issue and commit —
            # the account survives, stays empty, and the failure is visible.
            db.add(SyncIssue(id=uid(), org_id=account.org_id, linked_account_id=account.id,
                             model="sync", severity="error", message=f"real pull failed: {error}"))
            db.commit()
            audit(db, event_type="SYNC_ISSUE", org_id=account.org_id, actor=actor,
                  description=f"{account.integration}: {str(error)[:120]}")
            return {"integration": account.integration, "category": account.category,
                    "added": 0, "updated": 0, "unchanged": 0, "counts": {},
                    "skipped": 0, "errors": 1, "synced_at": iso(now())}
    else:
        rows = connector.generate_sample(account.category) or []
    wanted = _WANTED[account.category]

    settings = account.settings or {}
    selective = settings.get("selective_sync") or {}
    ignored_ids = set(settings.get("ignored_remote_ids") or [])

    outcome = {"added": 0, "updated": 0, "unchanged": 0}
    counts: dict[str, int] = {}
    skipped = 0
    errors = 0
    for row in rows:
        row_type = row.get("type") or ("ticket" if "title" in row else "contact")
        if row_type not in wanted:
            continue
        model = _MODEL_NAMES[row_type]
        if selective.get(model) is False:
            skipped += 1
            continue
        if str(row.get("remote_id") or "") in ignored_ids:
            skipped += 1
            continue
        loader = _LOADER[row_type]
        try:
            with db.begin_nested():        # savepoint: a bad row must not discard the caller's pending state
                verdict = loader(db, account, _apply_field_mappings(account, model, row))
        except Exception as error:  # noqa: BLE001 — record and keep syncing
            errors += 1
            db.add(SyncIssue(id=uid(), org_id=account.org_id, linked_account_id=account.id,
                             model=model, severity="error",
                             message=f"{row.get('remote_id')}: {error}"))
            continue
        outcome[verdict] += 1
        counts[model] = counts.get(model, 0) + 1

    account.last_synced_at = now()
    db.commit()

    usage(db, plane="data", kind="sync", org_id=account.org_id, units=float(len(rows)), credits=0,
          meta={"integration": account.integration, **counts})
    audit(db, event_type="ACCOUNT_SYNCED",
          description=(f"{account.category}/{account.integration}: "
                       f"+{outcome['added']} ~{outcome['updated']} ={outcome['unchanged']}"
                       + (f" ({errors} errors)" if errors else "")),
          org_id=account.org_id, actor=actor)

    if background is not None and (outcome["added"] or outcome["updated"]):
        emit(db, background, event="LinkedAccount.synced", org_id=account.org_id, payload_data={
            "linked_account_id": account.id, "integration": account.integration,
            "category": account.category, "added": outcome["added"],
            "updated": outcome["updated"], "counts": counts})

    return {"integration": account.integration, "category": account.category,
            "added": outcome["added"], "updated": outcome["updated"],
            "unchanged": outcome["unchanged"], "counts": counts,
            "skipped": skipped, "errors": errors,
            "synced_at": iso(now())}
