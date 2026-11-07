"""Connexa ORM models. One SQLite database backs all four planes:
data (Unified), link, agent (tool server), gateway (LLM router)."""
from datetime import datetime, timedelta

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .utils import now, uid


# ---------------------------------------------------------------- core tenants

class Org(Base):
    __tablename__ = "orgs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(140), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="core")  # core | agent | partner
    plan: Mapped[str] = mapped_column(String(20), default="free")
    claimed: Mapped[bool] = mapped_column(Boolean, default=False)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="default")
    scope: Mapped[str] = mapped_column(String(20), default="data")  # data | agent | gateway | admin
    key_secret: Mapped[str] = mapped_column(String(80))  # dev-mode convenience (plaintext)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_masked: Mapped[str] = mapped_column(String(40))
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    field_scopes: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"Ticket": {"exclude": [...]}}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---------------------------------------------------------------- link plane

class LinkToken(Base):
    __tablename__ = "link_tokens"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), index=True)
    token: Mapped[str] = mapped_column(String(80), unique=True)
    categories: Mapped[list] = mapped_column(JSON, default=list)          # ["ticketing","crm"]
    integration: Mapped[str | None] = mapped_column(String(60), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ready")      # ready | used | expired
    expires_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now() + timedelta(minutes=30))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class LinkedAccount(Base):
    __tablename__ = "linked_accounts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), index=True)
    category: Mapped[str] = mapped_column(String(20))                     # ticketing | crm
    integration: Mapped[str] = mapped_column(String(60))                  # connector slug
    end_user_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    account_token: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="live")
    credentials: Mapped[dict] = mapped_column(JSON, default=dict)
    linked_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sync_frequency_seconds: Mapped[int] = mapped_column(Integer, default=0)  # 0 = manual only
    settings: Mapped[dict] = mapped_column(JSON, default=dict)  # selective_sync, ignored_remote_ids


# ---------------------------------------------------------------- data plane

class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    linked_account_id: Mapped[str] = mapped_column(ForeignKey("linked_accounts.id"), index=True)
    remote_id: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(20), default="open")
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    assignee_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    remote_extra: Mapped[dict] = mapped_column(JSON, default=dict)   # RemoteData-style raw payload
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    linked_account_id: Mapped[str] = mapped_column(ForeignKey("linked_accounts.id"), index=True)
    remote_id: Mapped[str] = mapped_column(String(60))
    first_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    company: Mapped[str | None] = mapped_column(String(160), nullable=True)
    stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    owner_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    remote_extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Deal(Base):
    __tablename__ = "deals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    linked_account_id: Mapped[str] = mapped_column(ForeignKey("linked_accounts.id"), index=True)
    remote_id: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    stage: Mapped[str] = mapped_column(String(40), default="prospecting")
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    owner_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    contact_remote_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    remote_extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---------------------------------------------------------------- webhooks

class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), index=True)
    url: Mapped[str] = mapped_column(String(400))
    secret: Mapped[str] = mapped_column(String(80))
    events: Mapped[list] = mapped_column(JSON, default=list)  # ["*"] or specific
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("webhook_endpoints.id"), index=True)
    event: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---------------------------------------------------------------- agent plane

class ToolPack(Base):
    __tablename__ = "tool_packs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    agent_org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="Starter Pack")
    connectors: Mapped[list] = mapped_column(JSON, default=list)   # slugs
    per_connector_overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class RegisteredUser(Base):
    __tablename__ = "registered_users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    agent_org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), index=True)
    origin_user_id: Mapped[str] = mapped_column(String(120))
    origin_user_name: Mapped[str] = mapped_column(String(160))
    origin_company_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    credentials: Mapped[dict] = mapped_column(JSON, default=dict)  # connector_slug -> credential dict
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    idp_external_id: Mapped[str | None] = mapped_column(String(160), nullable=True)  # SCIM externalId
    active: Mapped[bool] = mapped_column(Boolean, default=True)   # SCIM deprovisioning flips this
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SecurityRule(Base):
    __tablename__ = "security_rules"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    agent_org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))      # CREDIT_CARD | EMAIL | SECRET_TOKEN | CUSTOM
    name: Mapped[str] = mapped_column(String(120))
    pattern: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(20), default="REDACT")   # ALLOW | REDACT | BLOCK
    scope: Mapped[str] = mapped_column(String(20), default="org")        # org | tool_pack
    tool_pack_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ToolAccessRequest(Base):
    __tablename__ = "tool_access_requests"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    agent_org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(160))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")   # pending | approved | rejected
    reviewer_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---------------------------------------------------------------- gateway plane

class GatewayModel(Base):
    __tablename__ = "gateway_models"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    vendor: Mapped[str] = mapped_column(String(60))
    family: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), default="available")
    input_cpt: Mapped[float] = mapped_column(Float, default=0.0)     # credits per 1k input tokens
    output_cpt: Mapped[float] = mapped_column(Float, default=0.0)    # credits per 1k output tokens
    context_window: Mapped[int] = mapped_column(Integer, default=128_000)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    provider_key: Mapped[str] = mapped_column(String(30), default="echo")  # echo | openai | anthropic


class RoutingPolicy(Base):
    __tablename__ = "routing_policies"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    strategy: Mapped[str] = mapped_column(String(30), default="priority")  # priority | balanced
    model_order: Mapped[list] = mapped_column(JSON, default=list)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Budget(Base):
    __tablename__ = "budgets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), index=True)
    period: Mapped[str] = mapped_column(String(20), default="monthly")
    credits_total: Mapped[int] = mapped_column(Integer, default=10_000)
    team_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # workforce team budget


# ---------------------------------------------------------------- workforce plane

class WorkforceTeam(Base):
    """A provisioning unit synced from the IdP (SCIM Groups) or created over the API.

    allowed_models ["*"] permits every catalog model; a concrete slug list is a
    per-team model allowlist enforced by the gateway. monthly_credit_budget is a
    hard per-team spend cap (402 once exhausted)."""
    __tablename__ = "workforce_teams"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    idp_external_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    tool_pack_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    allowed_models: Mapped[list] = mapped_column(JSON, default=list)      # ["*"] or slugs
    monthly_credit_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TeamMembership(Base):
    __tablename__ = "team_memberships"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("workforce_teams.id"), index=True)
    registered_user_id: Mapped[str] = mapped_column(ForeignKey("registered_users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="member")       # member | manager
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Skill(Base):
    """Shared skills library: named, versioned playbooks pushed to agent tooling."""
    __tablename__ = "skills"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), index=True)
    team_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # None = org-wide
    name: Mapped[str] = mapped_column(String(120))
    instructions: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class WorkforceMemory(Base):
    """Employee AI context: org-scoped memory rows agents can save and search."""
    __tablename__ = "workforce_memories"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), index=True)
    team_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    author: Mapped[str] = mapped_column(String(160), default="human")
    content: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SyncIssue(Base):
    """Sync health: one row per sync error, surfaced in /issues and the console."""
    __tablename__ = "sync_issues"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), index=True)
    linked_account_id: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[str] = mapped_column(String(40), default="sync")
    severity: Mapped[str] = mapped_column(String(20), default="error")    # error | warning
    message: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---------------------------------------------------------------- metering + audit

class UsageLog(Base):
    __tablename__ = "usage_logs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    plane: Mapped[str] = mapped_column(String(20))             # data | link | agent | gateway | dashboard
    org_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    agent_org_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    key_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    team_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(60))              # tool_call, llm_request, sync, ...
    credit_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    units: Mapped[float] = mapped_column(Float, default=0.0)
    credits: Mapped[float] = mapped_column(Float, default=0.0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditEntry(Base):
    __tablename__ = "audit_entries"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now)
    org_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    agent_org_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(120), default="system")
    event_type: Mapped[str] = mapped_column(String(60))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(60), nullable=True)
