"""Self-serve tenancy — the part that makes Connexa a product, not a demo.

Anyone can create a company account (email + password) and start using the
platform immediately: the signup returns live data + gateway API keys. Login
exchanges credentials for the account view. Plans are enforced for real at
connect time (free = 3 linked accounts); billing/upgrade hooks into a Stripe
payment link when one is configured.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .db import get_db
from .link import PLAN_LIMITS, _plan_limit_reached
from .metering import audit, used_credits_this_month
from .models import ApiKey, LinkedAccount, Org, OrgUser
from .security import create_api_key, resolve_key
from .utils import uid

router = APIRouter(prefix="/api/v1/org", tags=["org"])


# ---------------------------------------------------------------- passwords

def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _scheme, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                                n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:  # noqa: BLE001 — malformed hash -> refuse
        return False


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "org"


def _require_data_key(authorization: str | None = Header(default=None),
                      db: Session = Depends(get_db)):
    key, org = resolve_key(db, authorization)
    if key.scope not in ("data", "admin"):
        raise HTTPException(403, {"detail": "This endpoint requires a data or admin key."})
    return key, org


# ---------------------------------------------------------------- signup / login

class SignupRequest(BaseModel):
    email: str = Field(min_length=5, max_length=200)
    password: str = Field(min_length=8, max_length=200)
    company_name: str = Field(min_length=1, max_length=120)


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/signup", status_code=201)
def org_signup(data: SignupRequest, db: Session = Depends(get_db)):
    """Create a company account and get live API keys. Free plan starts immediately."""
    email = data.email.strip().lower()
    if not re.fullmatch(r"[\w.+-]+@[\w-]+\.[\w.-]+", email):
        raise HTTPException(400, {"detail": "Provide a valid email address."})
    if db.query(OrgUser).filter(OrgUser.email == email).first():
        raise HTTPException(409, {"detail": "This email already has an account — log in instead."})

    base_slug = _slugify(data.company_name)
    slug, n = base_slug, 1
    while db.query(Org).filter(Org.slug == slug).first():
        n += 1
        slug = f"{base_slug}-{n}"

    org = Org(id=uid(), name=data.company_name.strip(), slug=slug, kind="core",
              claimed=True, plan="free")
    db.add(org)
    db.flush()
    user = OrgUser(id=uid(), org_id=org.id, email=email,
                   password_hash=_hash_password(data.password),
                   display_name=email.split("@")[0])
    db.add(user)
    db.commit()

    data_key = create_api_key(db, org=org, name="default-data", scope="data")
    gateway_key = create_api_key(db, org=org, name="default-gateway", scope="gateway")
    audit(db, event_type="ORG_SIGNUP", description=f"{org.slug} <{email}>", org_id=org.id, actor=email)
    return {"org_id": org.id, "org": org.name, "slug": org.slug, "plan": org.plan,
            "email": email,
            "data_api_key": data_key.key_secret,
            "gateway_api_key": gateway_key.key_secret,
            "next_steps": ["Create a link token (POST /api/link-tokens) and connect an account",
                           "POST /api/v1/org/login retrieves this account view later"]}


@router.post("/login")
def org_login(data: LoginRequest, db: Session = Depends(get_db)):
    """Exchange email + password for the account view (keys masked — keys were shown at signup)."""
    user = db.query(OrgUser).filter(OrgUser.email == data.email.strip().lower()).first()
    if not user or not _verify_password(data.password, user.password_hash):
        raise HTTPException(401, {"detail": "Invalid email or password."})
    org = db.get(Org, user.org_id)
    keys = db.query(ApiKey).filter(ApiKey.org_id == org.id, ApiKey.active == True).all()  # noqa: E712
    audit(db, event_type="ORG_LOGIN", description=org.slug, org_id=org.id, actor=user.email)
    return {"org_id": org.id, "org": org.name, "plan": org.plan,
            "email": user.email,
            "api_keys": [{"name": k.name, "scope": k.scope, "masked": k.key_masked}
                         for k in keys],
            "note": "For security, full keys are shown once at signup."}


# ---------------------------------------------------------------- account view + billing

@router.get("/me")
def org_me(auth=Depends(_require_data_key), db: Session = Depends(get_db)):
    _key, org = auth
    _hit, limit = _plan_limit_reached(db, org)
    return {"org_id": org.id, "org": org.name, "slug": org.slug, "plan": org.plan,
            "linked_accounts": {"used": db.query(LinkedAccount).filter(LinkedAccount.org_id == org.id).count(),
                                "limit": limit},
            "gateway_credits_this_month": round(used_credits_this_month(db, org.id), 4)}


@router.get("/billing")
def billing(auth=Depends(_require_data_key), db: Session = Depends(get_db)):
    """Plan, enforced limits, this month's spend, and the upgrade link when configured."""
    _key, org = auth
    limits = PLAN_LIMITS.get(org.plan or "free", PLAN_LIMITS["free"])
    used = db.query(LinkedAccount).filter(LinkedAccount.org_id == org.id).count()
    credits = round(used_credits_this_month(db, org.id), 4)
    payment_link = os.getenv("CONNEXA_STRIPE_PAYMENT_LINK", "").strip()
    return {"plan": org.plan,
            "limits": limits,
            "usage": {"linked_accounts": used, "gateway_credits_this_month": credits},
            "upgrade_url": payment_link or None,
            "payments_configured": bool(payment_link),
            "note": ("Pay the upgrade link and your org's plan is raised automatically "
                     "by the billing webhook." if payment_link
                     else "Set CONNEXA_STRIPE_PAYMENT_LINK to enable self-serve upgrades.")}


@router.get("/billing/plan-limits")
def plan_limits(auth=Depends(_require_data_key), db: Session = Depends(get_db)):
    _key, _org = auth
    return {"plans": PLAN_LIMITS}
