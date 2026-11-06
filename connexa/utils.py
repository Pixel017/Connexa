"""Small shared helpers."""
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timezone


def uid() -> str:
    return uuid.uuid4().hex[:32]


def now() -> datetime:
    return datetime.now(timezone.utc)


def naive_now() -> datetime:
    """SQLite round-trips datetimes without tzinfo — compare with this."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def request_id() -> str:
    return str(uuid.uuid4())


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def new_token(prefix: str, nbytes: int = 24) -> str:
    return f"{prefix}_{secrets.token_hex(nbytes)}"


def mask(secret: str) -> str:
    if not secret:
        return ""
    return f"{secret[:9]}\u2026{secret[-4:]}"


def dumps(obj, limit: int | None = None) -> str:
    text = json.dumps(obj, default=str, ensure_ascii=False)
    if limit and len(text) > limit:
        text = text[:limit] + "\u2026"
    return text
