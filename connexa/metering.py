"""Audit trail + usage/credit metering (shared by every plane)."""
import re

from sqlalchemy.orm import Session

from .models import AuditEntry, Budget, UsageLog
from .utils import uid

CREDIT_WEIGHTS = {"low": 1.0, "medium": 3.0, "high": 10.0}


def audit(db: Session, *, event_type: str, description: str | None = None, actor: str | None = None,
          org_id: str | None = None, agent_org_id: str | None = None, ip: str | None = None,
          key_id: str | None = None) -> None:
    """Audit trail row. Actor defaults to the presenting key id when available."""
    if actor is None:
        actor = f"key:{key_id[:12]}" if key_id else "system"
    db.add(AuditEntry(id=uid(), event_type=event_type, description=description, actor=actor,
                      org_id=org_id, agent_org_id=agent_org_id, ip=ip))
    db.commit()


def usage(db: Session, *, plane: str, kind: str, units: float = 0.0, credit_type: str | None = None,
          credits: float | None = None, org_id: str | None = None, agent_org_id: str | None = None,
          key_id: str | None = None, meta: dict | None = None) -> float:
    if credits is None:
        credits = round(units * CREDIT_WEIGHTS.get(credit_type or "low", 1.0), 4)
    db.add(UsageLog(id=uid(), plane=plane, kind=kind, units=units, credit_type=credit_type,
                    credits=credits, org_id=org_id, agent_org_id=agent_org_id, key_id=key_id,
                    meta=meta or {}))
    db.commit()
    return credits


def used_credits_this_month(db: Session, org_id: str) -> float:
    from .utils import now

    start = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = (
        db.query(UsageLog)
        .filter(UsageLog.plane == "gateway", UsageLog.org_id == org_id, UsageLog.ts >= start)
        .all()
    )
    return sum(r.credits for r in rows)


def check_gateway_budget(db: Session, org_id: str) -> int:
    budget = db.query(Budget).filter(Budget.org_id == org_id).first()
    if not budget:
        budget = Budget(id=uid(), org_id=org_id, credits_total=10_000)
        db.add(budget)
        db.commit()
    used = used_credits_this_month(db, org_id)
    return int(budget.credits_total - used)


_REL = re.compile(r"\s+")


def scrub(text: str) -> str:
    return _REL.sub(" ", text or "").strip()
