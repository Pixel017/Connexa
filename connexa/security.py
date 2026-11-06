"""Bearer-key authentication, webhook HMAC signing, admin cookie."""
import hashlib
import hmac

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from .db import get_db
from .models import ApiKey, Org
from .config import settings


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_api_key(db: Session, *, org: Org, name: str, scope: str, is_test: bool = False,
                   prefix: str | None = None) -> ApiKey:
    from .utils import new_token, mask, uid

    prefix_map = {"data": "cxk", "agent": "cxa", "gateway": "cxg", "admin": "cxr"}
    raw = new_token(prefix or prefix_map.get(scope, "cxk"))
    key = ApiKey(
        id=uid(), org_id=org.id, name=name, scope=scope, is_test=is_test,
        key_secret=raw, key_hash=_hash_key(raw), key_masked=mask(raw),
    )
    db.add(key)
    db.commit()
    return key


def resolve_key(db: Session, authorization: str | None) -> tuple[ApiKey, Org]:
    """Every bearer-keyed lane funnels through here (like a single auth middleware)."""
    if not authorization:
        raise HTTPException(401, {"detail": "Invalid bearer token header. No credentials provided."})
    raw = authorization.removeprefix("Bearer ").strip()
    if not raw:
        raise HTTPException(401, {"detail": "Invalid bearer token header. No credentials provided."})
    key = db.query(ApiKey).filter(ApiKey.key_hash == _hash_key(raw), ApiKey.active == True).first()  # noqa: E712
    if not key:
        raise HTTPException(401, {"detail": "Invalid bearer token."})
    org = db.get(Org, key.org_id)
    return key, org


def require_scope(scope: str):
    def dep(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
        key, org = resolve_key(db, authorization)
        if key.scope != scope:
            raise HTTPException(403, {"detail": f"This endpoint requires a '{scope}' API key."})
        return key, org
    return dep


# data plane accepts data OR admin keys
def data_plane_key(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    key, org = resolve_key(db, authorization)
    if key.scope not in ("data", "admin"):
        raise HTTPException(403, {"detail": "This endpoint requires a data-plane API key."})
    return key, org


def webhook_signature(secret: str, raw_body: bytes) -> str:
    """HMAC-SHA256 hex over the raw body — the same raw-body discipline the big platforms require."""
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def admin_cookie_ok(request: Request) -> bool:
    return request.cookies.get("cx_admin") == settings.admin_cookie_value
