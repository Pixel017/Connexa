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
from ..connectors.registry import registry

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
        "blurb": ("Approve what agents and employees can do — then watch every action in one "
                  "audit trail. Shipping today inside the console; the full IdP and skills "
                  "library are on the public roadmap."),
        "features": [
            ("01", "Approvals before access",
             "Sensitive tool calls open a ToolAccessRequest; a human approves or rejects it "
             "from the console. Agents wait politely — and tell you what they wanted."),
            ("02", "Data-loss prevention built in",
             "SecurityRule runs on agent traffic: REDACT or BLOCK credit cards, emails, "
             "secret tokens, or your own regex patterns, org-wide or per tool pack."),
            ("03", "Spend control for every org",
             "Gateway budgets and credit metering cap what any team's agents can burn — "
             "visible live from the console."),
        ],
        "code_title": "On the public roadmap",
        "code": (
            "- IdP-driven provisioning (Okta / Entra SCIM) mapping teams -> tool packs\n"
            "- Per-team model allowlists + shared skills library\n"
            "- DLP on outbound model traffic (today it guards agent tool traffic)\n"
            "- Scheduled background sync (today: API- and console-triggered)"),
    },
}


# --------------------------------------------------------------- pages

@router.get("/", include_in_schema=False)
def landing(request: Request):
    connectors = [{"slug": c.slug, "name": c.name, "real": c.real_lane_ready}
                  for c in registry.list_connectors()]
    return _page(request, "landing.html", {"connectors": connectors})


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
        if connector.generate_sample("ticketing") or connector.generate_sample("crm"):
            entry["categories"] = []
            if connector.generate_sample("ticketing"):
                entry["categories"].append("ticketing")
            if connector.generate_sample("crm"):
                entry["categories"].append("crm")
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
