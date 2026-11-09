"""The public website: landing, four product pages, pricing, security, integrations.

All pages are server-rendered from templates — no JS framework, no build step.
Routes are excluded from the OpenAPI schema; the API surface stays at /docs.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..common_models import DATA_CATEGORIES
from ..connectors.registry import registry
from ..features import FEATURES, counts

router = APIRouter(tags=["site"])
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=TEMPLATE_DIR)


def _page(request: Request, template: str, ctx: dict):
    return templates.TemplateResponse(request, template, ctx)


# --------------------------------------------------------------- product pages

PRODUCTS: dict[str, dict] = {
    "unified": {
        "eyebrow": "Connexa Unified",
        "title": "Ship data integrations through one Common Model API",
        "blurb": ("Connect your customers' accounts once, serve normalized business data "
                  "forever. Tickets, contacts and deals from every provider in a category, "
                  "through one schema you design against."),
        "features": [
            ("01", "Link once, serve forever",
             "POST /api/link-tokens rides your end user through the hosted Connexa Link flow. "
             "Out comes an account token scoped to one linked account, in one category."),
            ("02", "Sync, not scrape",
             "The sync engine pulls provider payloads into Common Model rows by remote_id — "
             "idempotent, diff-aware (added / updated / unchanged), events fired on change."),
            ("03", "Write back safely",
             "Discover the schema with /meta/write-schema, then create and patch normalized rows "
             "with your data key + X-Account-Token. Raw provider payloads ride along in remote_data."),
        ],
        "code_title": "End-to-end in four calls",
        "code": (
            "curl -X POST $HOST/api/link-tokens -H 'Authorization: Bearer $DATA_KEY'\n"
            "# open the returned /link/{token} — your user connects in the browser\n"
            "curl -X POST $HOST/api/ticketing/v1/accounts/$PUBLIC_TOKEN/sync \\\n"
            "  -H 'Authorization: Bearer $DATA_KEY'\n"
            "curl $HOST/api/ticketing/v1/tickets -H 'Authorization: Bearer $DATA_KEY' \\\n"
            "  -H 'X-Account-Token: $ACCOUNT_TOKEN'   # normalized rows + remote_data"),
    },
    "agent-handler": {
        "eyebrow": "Connexa Agent Handler",
        "title": "Give your agent real accounts — safely",
        "blurb": ("One signup endpoint bootstraps a full MCP tool server for your agent: OAuth "
                  "social-login for humans, scoped tool packs, DLP rules, and audit on everything."),
        "features": [
            ("01", "Self-serve bootstrap",
             "POST /api/v1/agent/signup/ returns credentials, a tool pack and a claim link — "
             "no dashboard required. The MCP surface speaks JSON-RPC over plain JSON or SSE."),
            ("02", "Real OAuth, real re-auth contracts",
             "Every connector exposes authenticate_<slug> plus a machine-actionable "
             "reauth_required payload, so agents fix their own expired credentials."),
            ("03", "Scoped, filtered, audited",
             "Tool packs constrain what agents can reach; SecurityRule runs REDACT / BLOCK "
             "(credit cards, emails, secrets, custom regex); sensitive tool calls require "
             "human approval via ToolAccessRequest."),
        ],
        "code_title": "Agent bootstrap",
        "code": (
            "curl -X POST $HOST/api/v1/agent/signup/   # self-serve, no auth\n"
            "# → {api_key, tool_pack_id, registered_user_id, claim_link}\n"
            "curl -X POST $HOST/api/v1/tool-packs/$PACK/registered-users/$USER/mcp/ \\\n"
            "  -H 'Authorization: Bearer $AGENT_KEY' -d '{\"method\": \"tools/list\"}'"),
    },
    "gateway": {
        "eyebrow": "Connexa Gateway",
        "title": "Route every LLM call through one API",
        "blurb": ("An OpenAI-compatible surface in front of every model you run — with routing "
                  "policies, spend metering in credits, budgets that actually stop spending."),
        "features": [
            ("01", "One surface, every model",
             "POST /v1/chat/completions (streaming supported) and /v1/responses against the "
             "model catalog — echo models included for deterministic dev/test."),
            ("02", "Routing policies",
             "Priority or balanced strategies over an ordered model list; policies are org-"
             "scoped objects you create over the API and manage from the console."),
            ("03", "Budgets that bite",
             "Every request is metered in credits (per-model input/output rates). Set a monthly "
             "budget — /v1/budget — and calls are refused, not merely logged, once it's spent."),
        ],
        "code_title": "One call, any model",
        "code": (
            "curl -X POST $HOST/v1/chat/completions -H 'Authorization: Bearer $GATEWAY_KEY' \\\n"
            "  -d '{\"model\": \"gpt-4o-mini\", \"messages\": [{\"role\": \"user\", "
            "\"content\": \"Summarize my open tickets\"}]}'\n"
            "curl $HOST/v1/budget -H 'Authorization: Bearer $GATEWAY_KEY'"),
    },
    "workforce": {
        "eyebrow": "Connexa for Workforce",
        "title": "Govern the AI your whole company uses",
        "blurb": ("Provision teams from your IdP, approve exactly which models each team may call, cap what "
                  "they spend, scan everything leaving for a model — and give every agent shared skills and "
                  "memory. All enforced in-path, all in one audit trail."),
        "features": [
            ("01", "IdP / SCIM 2.0 provisioning",
             "Point Okta or Entra at /scim/v2. Directory users land as registered users; groups become teams. "
             "Membership in a team is what grants its tool pack, model allowlist and budget — deprovision once, "
             "everywhere."),
            ("02", "Control model access and spend",
             "Per-team model allowlists (the gateway refuses unapproved models and filters failover chains) plus "
             "hard monthly credit budgets per team — calls are refused, not merely logged, once a team's budget "
             "is spent."),
            ("03", "DLP on outbound model traffic",
             "Prompts are scanned before they reach any model: REDACT or BLOCK credit cards, emails, secret "
             "tokens, or your own regex — the same rule engine that guards agent tool traffic, now on the "
             "gateway."),
            ("04", "Shared skills + employee AI memory",
             "A versioned skills library (playbooks every agent follows) and org-wide memory — exposed over REST "
             "and as memory_save / memory_search / skills_list MCP tools inside every agent's tool list."),
        ],
        "code_title": "Provision a governed team in one SCIM call",
        "code": (
            "POST /scim/v2/Groups\n"
            '{"displayName": "Support", "externalId": "okta-123",\n'
            ' "members": [{"value": "scim-user-id"}],\n'
            ' "connexa_extensions": {\n'
            '   "allowed_models": ["connexa-echo"],\n'
            '   "monthly_credit_budget": 500}}\n'
            "# then the gateway enforces it:\n"
            "POST /v1/chat/completions  -H 'X-Connexa-Team: Support'"),
    },
}


# --------------------------------------------------------------- pages

@router.get("/", include_in_schema=False)
def landing(request: Request):
    connectors = [{"slug": c.slug, "name": c.name, "real": c.real_lane_ready}
                  for c in registry.list_connectors()]
    return _page(request, "landing.html", {"connectors": connectors})


@router.get("/features", include_in_schema=False)
def features_page(request: Request):
    live, roadmap = counts()
    return _page(request, "features.html",
                 {"groups": FEATURES, "live": live, "roadmap": roadmap})


@router.get("/unified", include_in_schema=False)
def product_unified(request: Request):
    return _page(request, "product.html", {"p": PRODUCTS["unified"]})


@router.get("/agent-handler", include_in_schema=False)
def product_agent_handler(request: Request):
    return _page(request, "product.html", {"p": PRODUCTS["agent-handler"]})


@router.get("/gateway", include_in_schema=False)
def product_gateway(request: Request):
    return _page(request, "product.html", {"p": PRODUCTS["gateway"]})


@router.get("/workforce", include_in_schema=False)
def product_workforce(request: Request):
    return _page(request, "product.html", {"p": PRODUCTS["workforce"]})


@router.get("/pricing", include_in_schema=False)
def pricing(request: Request):
    return _page(request, "pricing.html", {})


@router.get("/security", include_in_schema=False)
def security(request: Request):
    return _page(request, "security.html", {})


@router.get("/integrations", include_in_schema=False)
def integrations(request: Request):
    tool_connectors, data_connectors = [], []
    for connector in registry.list_connectors():
        entry = {"name": connector.name, "slug": connector.slug,
                 "auth": getattr(connector, "auth_type", "token"),
                 "real": connector.real_lane_ready}
        categories = [c for c in DATA_CATEGORIES if connector.generate_sample(c)]
        if categories:
            entry["categories"] = categories
            data_connectors.append(entry)
        else:
            tool_connectors.append(entry)
    return _page(request, "integrations.html",
                 {"tool_connectors": tool_connectors, "data_connectors": data_connectors})


@router.get("/docs", include_in_schema=False)
def docs_entry(request: Request):
    return RedirectResponse("/docs/api", status_code=303)


@router.get("/docs/api", include_in_schema=False)
def docs_api(request: Request):
    return RedirectResponse("/docs", status_code=303)
