"""Event bus + HMAC webhook dispatch (raw-body signing, delivery receipts, retries).

Delivery policy: an event fans out to every matching endpoint. A failed delivery
is retried with exponential backoff (30s, 2m, 10m — MAX_ATTEMPTS total tries);
the scheduler re-drives due retries, so a temporarily-down receiver never
silently loses an event. Every attempt is recorded on the delivery row.
"""
from __future__ import annotations

import json
import httpx
from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from .models import WebhookDelivery, WebhookEndpoint
from .security import webhook_signature
from .utils import now, iso, uid

MAX_ATTEMPTS = 4                      # 1 initial try + 3 retries
BACKOFF_SECONDS = [30, 120, 600]      # before retry 2, 3, 4


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
            delivery = WebhookDelivery(id=uid(), endpoint_id=endpoint.id, event=event, payload=payload,
                                       ok=False, attempts=0, next_attempt_at=now())
            db.add(delivery)
            db.commit()
            background.add_task(_deliver_async, delivery.id, raw)


def _http_post(url: str, secret: str, event: str, raw_body: bytes) -> int | None:
    signature = webhook_signature(secret, raw_body)
    with httpx.Client(timeout=6.0) as client:
        response = client.post(
            url, content=raw_body,
            headers={"Content-Type": "application/json",
                     "X-Connexa-Signature": "sha256=" + signature,
                     "X-Connexa-Event": event})
    return response.status_code


def attempt_delivery(delivery_id: str, raw_body: bytes | None = None) -> bool:
    """One delivery attempt (called from the background task or the retry scheduler).

    Records the attempt, schedules the next backoff on failure, returns success."""
    from .db import SessionLocal

    session = SessionLocal()
    try:
        delivery = session.get(WebhookDelivery, delivery_id)
        if delivery is None or delivery.ok:
            return bool(delivery.ok) if delivery else False
        endpoint = session.get(WebhookEndpoint, delivery.endpoint_id)
        if endpoint is None:
            delivery.next_attempt_at = None     # endpoint deleted: stop retrying
            session.commit()
            return False
        raw = raw_body if raw_body is not None else json.dumps(delivery.payload, default=str).encode()
        status = None
        try:
            status = _http_post(endpoint.url, endpoint.secret, delivery.event, raw)
            ok = 200 <= status < 300
        except Exception:  # noqa: BLE001 — connection refused/DNS/timeouts are retryable failures
            ok = False
        delivery.status_code = status
        delivery.ok = ok
        delivery.attempts = (delivery.attempts or 0) + 1
        if ok or delivery.attempts >= MAX_ATTEMPTS:
            delivery.next_attempt_at = None      # delivered, or dead: no more retries
        else:
            delivery.next_attempt_at = _next_retry_time(delivery.attempts)
        session.commit()
        return ok
    finally:
        session.close()


def _next_retry_time(attempts_so_far: int):
    from datetime import timedelta

    index = min(attempts_so_far - 1, len(BACKOFF_SECONDS) - 1)
    return now() + timedelta(seconds=BACKOFF_SECONDS[index])


def _deliver_async(delivery_id: str, raw_body: bytes) -> None:
    try:
        attempt_delivery(delivery_id, raw_body)
    except Exception:  # noqa: BLE001 — never raise out of a background task
        pass


def due_retries(db: Session, limit: int = 25) -> list[WebhookDelivery]:
    """Failed deliveries whose backoff elapsed and attempts remain."""
    from sqlalchemy import and_

    return (db.query(WebhookDelivery)
            .filter(and_(WebhookDelivery.ok == False,                        # noqa: E712
                         WebhookDelivery.next_attempt_at.isnot(None),
                         WebhookDelivery.next_attempt_at <= now(),
                         WebhookDelivery.attempts < MAX_ATTEMPTS))
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(limit).all())


def process_due_retries(db: Session) -> int:
    """Scheduler hook: re-attempt due deliveries. Returns number attempted."""
    attempted = 0
    for delivery in due_retries(db):
        if attempt_delivery(delivery.id):
            attempted += 1
    return attempted
