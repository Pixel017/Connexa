"""Event bus + HMAC webhook dispatch (raw-body signing, with delivery receipts)."""
from __future__ import annotations

import json
import httpx
from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from .models import WebhookDelivery, WebhookEndpoint
from .security import webhook_signature
from .utils import now, iso, uid


def emit(db: Session, background: BackgroundTasks, *, event: str, org_id: str, payload_data: dict) -> None:
    """Persist a delivery row per matching endpoint, then deliver signed after-response."""
    endpoints = (
        db.query(WebhookEndpoint)
        .filter(WebhookEndpoint.org_id == org_id, WebhookEndpoint.active == True)  # noqa: E712
        .all()
    )
    payload = {"event": event, "fired_at": iso(now()), "data": payload_data}
    raw = json.dumps(payload, default=str).encode()
    for endpoint in endpoints:
        if "*" in endpoint.events or event in endpoint.events:
            db.add(WebhookDelivery(id=uid(), endpoint_id=endpoint.id, event=event, payload=payload, ok=False))
            db.commit()
            background.add_task(_deliver, endpoint.id, endpoint.url, endpoint.secret, event, raw)


def _deliver(endpoint_id: str, url: str, secret: str, event: str, raw_body: bytes) -> None:
    status = None
    ok = False
    signature = webhook_signature(secret, raw_body)
    try:
        with httpx.Client(timeout=6.0) as client:
            response = client.post(
                url, content=raw_body,
                headers={"Content-Type": "application/json",
                         "X-Connexa-Signature": "sha256=" + signature,
                         "X-Connexa-Event": event})
        status = response.status_code
        ok = 200 <= response.status_code < 300
    except Exception:  # noqa: BLE001
        ok = False
    from .db import SessionLocal
    session = SessionLocal()
    try:
        from .models import WebhookDelivery as WD
        delivery = (
            session.query(WD).filter(WD.endpoint_id == endpoint_id, WD.event == event, WD.ok == False)  # noqa: E712
            .order_by(WD.created_at.desc()).first()
        )
        if delivery:
            delivery.status_code = status
            delivery.ok = ok
            session.commit()
    finally:
        session.close()
