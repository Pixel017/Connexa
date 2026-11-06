"""First-boot seeding: core org, API keys, gateway model catalog, routing policy, budget."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .config import settings
from .models import (ApiKey, Budget, GatewayModel, Org, RoutingPolicy)
from .security import create_api_key
from .metering import audit
from .utils import uid


def seed(db: Session) -> dict:
    existing = db.query(Org).filter(Org.kind == "core").first()
    if existing:
        key = (db.query(ApiKey).filter(ApiKey.org_id == existing.id, ApiKey.is_test == True)  # noqa: E712
               .order_by(ApiKey.created_at).first())
        return {"org": existing, "demo_key": key.key_secret if key else None,
                "gateway_key": _first_key(db, existing, "gateway")}

    org = Org(id=uid(), name="Acme Inc", slug="acme-inc", kind="core", claimed=True)
    db.add(org)
    db.flush()

    demo_key = create_api_key(db, org=org, name="sandbox", scope="data", is_test=True)
    create_api_key(db, org=org, name="production", scope="data")
    gateway_key = create_api_key(db, org=org, name="gateway-default", scope="gateway")

    models = [
        ("connexa-echo", "connexa", "echo", "available", 0.0, 0.0, "echo", 32_000,
         {"reasoning": False, "modality": "text"}),
        ("gpt-4o-mini", "openai", "openai-gpt", "available", 0.05, 0.15, "openai", 128_000,
         {"reasoning": False, "modality": "text"}),
        ("gpt-4o", "openai", "openai-gpt", "available", 0.4, 1.6, "openai", 128_000,
         {"reasoning": False, "modality": "text"}),
        ("anthropic/claude-3-5-sonnet", "anthropic", "claude", "available", 0.24, 1.2, "anthropic", 200_000,
         {"reasoning": False, "modality": "text"}),
        ("deepseek-deep", "openai-compat", "deepseek", "preview", 0.03, 0.09, "openai", 64_000,
         {"reasoning": True, "modality": "text"}),
    ]
    for slug, vendor, family, status, in_cpt, out_cpt, pkey, ctx, caps in models:
        db.add(GatewayModel(id=uid(), slug=slug, vendor=vendor, family=family, status=status,
                            input_cpt=in_cpt, output_cpt=out_cpt, context_window=ctx,
                            capabilities=caps, provider_key=pkey))

    db.add(RoutingPolicy(id=uid(), org_id=org.id, name="quality-first",
                         strategy="priority",
                         model_order=["gpt-4o", "anthropic/claude-3-5-sonnet", "gpt-4o-mini", "connexa-echo"],
                         is_default=True))
    db.add(RoutingPolicy(id=uid(), org_id=org.id, name="cheap-first",
                         strategy="priority",
                         model_order=["gpt-4o-mini", "deepseek-deep", "connexa-echo"]))
    db.add(Budget(id=uid(), org_id=org.id, period="monthly",
                  credits_total=settings.monthly_budget_credits))
    db.commit()
    audit(db, event_type="ORG_SEEDED", description=org.slug, org_id=org.id)
    return {"org": org, "demo_key": demo_key.key_secret, "gateway_key": gateway_key.key_secret}


def _first_key(db: Session, org: Org, scope: str) -> str | None:
    row = (db.query(ApiKey).filter(ApiKey.org_id == org.id, ApiKey.scope == scope)
           .order_by(ApiKey.created_at).first())
    return row.key_secret if row else None
