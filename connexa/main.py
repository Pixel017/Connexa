"""Connexa app assembly: one FastAPI process hosts all four planes."""
from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .db import init_db, SessionLocal
from .site.routes import router as site_router
from .unified import router as unified_router
from .link import router as link_router, api_router as link_api_router
from .agent.handler import router as agent_router
from .agent.mcp import router as mcp_router
from .gateway.api import router as gateway_router
from .dashboard.routes import router as dashboard_router
from .oauth_routes import router as oauth_router

app = FastAPI(
    title="Connexa",
    version="0.1.0",
    description=("A unified business connectivity platform: one Common Model data plane, "
                 "a link flow for end users, an MCP tool server for agents, and an LLM "
                 "model router — in a single process."),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=False,
)

# Order matters: site owns the public marketing pages ("/", /unified, /pricing, ...);
# dashboard covers "/", "/login", "/claim/*" (its "/" redirects to /dashboard); link
# covers "/link/*"; unified covers "/api/*"; agent covers "/api/v1/*"; gateway covers
# "/v1/*"; oauth covers "/oauth/*/callback" and the registered-user oauth start endpoint.
app.include_router(site_router)
app.include_router(dashboard_router)
app.include_router(link_router)
app.include_router(link_api_router)
app.include_router(oauth_router)
app.include_router(unified_router)
app.include_router(agent_router)
app.include_router(mcp_router)
app.include_router(gateway_router)


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "connexa", "version": "0.1.0"}


_llms = """# Connexa

Connexa is a self-contained business connectivity platform. Four planes in one process:

- llms-full.txt for machine-readable endpoint docs
- /docs for the OpenAPI UI
- Data plane: /api/{ticketing,crm}/v1/... with Bearer data key + X-Account-Token
- Link flow: POST /api/link-tokens (connect a linked account via the browser)
- Agent Handler: POST /api/v1/agent/signup/ (self-serve agent bootstrap, MCP surface)
- Gateway: /v1/chat/completions (OpenAI-compatible model routing)

Connectors (starter tool pack): weather, wikipedia, jira, slack, notion, github, gmail, helpdesk.
Every connector exposes authenticate_<slug> (sign-in) + <slug>__validate_credential; tools called
without a connection return the machine-actionable reauth_required payload.

Agents should call the agent signup endpoint first; it returns a claim link for a human
to take ownership of the new organization.
"""

Full_LLMs = """# Connexa — full map (llms)

## Quickstart for agents
POST /api/v1/agent/signup/ (no auth) -> {"api_key", "tool_pack_id", "registered_user_id", "claim_link"}
MCP surface: POST /api/v1/tool-packs/{tool_pack_id}/registered-users/{registered_user_id}/mcp/
tool search: POST .../search/ with {"intent": "..."}
security rules: GET /api/v1/security/rules/ ; mutate via POST /api/v1/security/rules/

## Data plane for customers
POST /api/link-tokens -> open returned /link/{token} in a browser -> connect (initial sync runs automatically)
POST /api/{category}/v1/account-token/{public_token}
POST /api/{category}/v1/accounts/{public_token}/sync -> on-demand idempotent sync (added/updated/unchanged + counts)
GET/POST/PATCH /api/ticketing/v1/tickets ; /api/crm/v1/contacts ; /api/crm/v1/deals
GET /api/ticketing/v1/meta/write-schema before writes; POST /api/ticketing/v1/passthrough for raw provider calls

## Gateway
GET /v1/models ; POST /v1/chat/completions (stream supported) ; POST /v1/responses
GET /v1/budget ; GET /v1/usage ; routing policies at /v1/routing-policies
"""


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(_llms)


@app.get("/llms-full.txt", include_in_schema=False)
async def llms_full_txt():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(Full_LLMs)


@app.on_event("startup")
def boot() -> None:
    init_db()
    db: Session = SessionLocal()
    try:
        from .seed import seed
        seeded = seed(db)
        db.commit()
    finally:
        db.close()
    # Print dev keys once per boot (dev convenience; production stores hashes only).
    if seeded and seeded.get("demo_key"):
        print("============================================================")
        print(" Connexa booted. Seeded org keys (dev-mode, shown each boot):")
        print(f"   DATA   API key : {seeded['demo_key']}")
        print(f"   GATEWAY API key: {seeded['gateway_key']}")
        print(f"   Admin console  : /login  (admin token: dev-admin by default)")
        print("   Docs           : /docs   |  agent map: /llms.txt")
        print("============================================================")
