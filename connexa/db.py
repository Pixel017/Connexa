"""SQLite + SQLAlchemy setup."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from .config import settings

engine = create_engine(f"sqlite:///{settings.db_path}", connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from . import models  # noqa: F401  (register mappings)

    Base.metadata.create_all(engine)
    _ensure_columns()


_NEW_COLUMNS = {
    # table: [(column, full ALTER ddl), ...] — SQLite ALTER TABLE ADD COLUMN, applied
    # idempotently so upgraded databases (e.g. a shipped connexa.db) keep working.
    "linked_accounts": [
        ("sync_frequency_seconds", "INTEGER NOT NULL DEFAULT 0"),
        ("settings", "JSON NOT NULL DEFAULT '{}'"),
    ],
    "link_tokens": [
        ("company_name", "VARCHAR(120)"),
        ("logo_url", "VARCHAR(400)"),
        ("accent_color", "VARCHAR(9)"),
        ("welcome_text", "VARCHAR(300)"),
    ],
    "webhook_deliveries": [
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("next_attempt_at", "DATETIME"),
    ],
    "api_keys": [
        ("field_scopes", "JSON"),
    ],
    "registered_users": [
        ("idp_external_id", "VARCHAR(160)"),
        ("active", "BOOLEAN NOT NULL DEFAULT 1"),
    ],
    "budgets": [
        ("team_id", "VARCHAR(32)"),
    ],
    "usage_logs": [
        ("team_id", "VARCHAR(32)"),
    ],
}


def _ensure_columns() -> None:
    from sqlalchemy import text

    with engine.connect() as connection:
        for table, columns in _NEW_COLUMNS.items():
            existing = {row[1] for row in
                        connection.execute(text(f"PRAGMA table_info({table})"))}
            if not existing:
                continue  # table not created yet (fresh DB) — create_all handled it
            for column, ddl in columns:
                if column not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        connection.commit()
