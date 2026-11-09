# Connexa

![CI](https://github.com/Pixel017/Connexa/actions/workflows/ci.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.13%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![docker](https://img.shields.io/badge/docker-ready-2496ED)

**A unified business connectivity platform in one process** — original code implementing the four architectural pillars of a commercial "unified API" product:

1. **Unified data plane** — one normalized Common Model per category (Ticketing, CRM, HRIS, Accounting, ATS, File Storage), backed by any connected provider
2. **Link flow** — short-lived link tokens + an end-user connection screen handing back a public token
3. **Agent Handler** — self-serve agent orgs, tool packs, an MCP tool server, and a security gateway
4. **Gateway** — an OpenAI-compatible LLM router with routing policies, failover, budgets, and credit metering

Plus the SaaS layer that makes it a product, not a demo:

- **Self-serve tenancy** — anyone signs up with email + password and gets live API keys instantly
- **Enforced plans** — the free plan's limits are actually enforced (402 at connect time), with a billing surface and a Stripe upgrade hook
- **A real data lane** — `github-issues` syncs *real* GitHub repository issues into the Ticket Common Model (simulated connectors are always honestly labeled `demo`)

All of it runs from a single Python process with SQLite. No build step, no npm, no external services required.

---

## Quickstart (Windows)

```powershell
cd D:\Business\connexa
python -m venv .venv            # already present
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python.exe run.py
```

**Or Docker (any OS):**

```bash
docker compose up --build       # http://localhost:8000 — SQLite persisted in a named volume
```

Deploying to a server (HTTPS, backups, upgrades): **[`DEPLOY.md`](DEPLOY.md)**.

Then open **http://127.0.0.1:8000** .

| Surface | URL |
|---|---|
| **Marketing site** | `/` (landing) · `/unified` · `/agent-handler` · `/gateway` · `/workforce` · `/pricing` · `/security` · `/integrations` |
| API docs (OpenAPI UI) | `/docs` |
| Agent map for LLMs | `/llms.txt`, `/llms-full.txt` |
| Admin console | `/login` (token: `dev-admin` or `CONNEXA_ADMIN_TOKEN`) |
| Health | `/healthz` |

On first boot the platform seeds a demo org (**Acme Inc**) and prints its keys to the console:

```
DATA   API key : cxk_…   (data plane)
GATEWAY API key: cxg_…   (model router)
```

Agent keys are **not** pre-seeded — every agent self-registers at `POST /api/v1/agent/signup/` and receives its own `cxa_…` key.

> **Dev-mode honesty note:** keys are stored hashed at request time, but the plaintext is kept in SQLite in dev mode so you don't lose them (`SELECT key_secret FROM api_keys`). Clear that column or wire a vault for real deployments.

Delete `connexa.db` and restart to re-seed from scratch.

---

## The four planes

### 1. Unified data plane (`/api/{category}/v1/...`)

Auth: `Authorization: Bearer <cxk_… data key>` + `X-Account-Token: <cxt_…>` per linked account.

```bash
DATA=cxk_your_data_key
curl -H "Authorization: Bearer $DATA" -X POST localhost:8000/api/link-tokens \
     -d '{"categories": ["ticketing"]}'
# -> open the returned /link/<token> URL in a browser, connect an integration,
#    it hands you a public_token cxt_…

curl -X POST localhost:8000/api/ticketing/v1/account-token/cxt_public_token \
     -H "Authorization: Bearer $DATA"          # -> { "account_token": "cxt_…" }

curl -H "Authorization: Bearer $DATA" -H "X-Account-Token: cxt_…" \
     localhost:8000/api/ticketing/v1/tickets
```

Key endpoints:

| Endpoint | Purpose |
|---|---|
| `GET  /api/{cat}/v1/{model}` | normalized, cursor-paginated list (`_next` cursor) |
| `GET  /api/ticketing/v1/tickets/{id}` | single row with `remote_data` raw payload embedded |
| `POST /api/ticketing/v1/tickets` | validated create |
| `PATCH /api/ticketing/v1/tickets/{id}` | validated update |
| `GET  /api/ticketing/v1/meta/write-schema` | discover fields + enums **before** writing |
| `POST /api/ticketing/v1/passthrough` | escape hatch: raw method/path to the provider's own API |
| `POST /api/{cat}/v1/account-token/{public_token}` | exchange public token for account token |
| `POST /api/{cat}/v1/accounts/{public_token}/sync` | **on-demand sync**: pulls provider payloads into Common Model rows — idempotent, diff-aware (`added`/`updated`/`unchanged`), fires `LinkedAccount.synced` |
| `POST /api/{cat}/v1/accounts/{public_token}/sync-frequency` | **scheduled background sync** per account (0 = manual) |
| `GET  /api/{cat}/v1/issues` | open sync issues (health surface) |
| `DELETE /api/{cat}/v1/accounts/{public_token}` | **erasure**: removes the account and every derived row |
| `GET  /api/linked-accounts` | inventory of connected accounts |

Categories today: **ticketing** (`Ticket`), **crm** (`Contact`, `Deal`), **hris** (`Employee`),
**accounting** (`Invoice`), **ats** (`Candidate`), **filestorage** (`File`, `Folder`) — six public
unified categories. Writes are validated against the Common Model — wrong enum values get a 400
with the allowed choices in the body.

### 2. Link flow (`/link/...`)

- Dashboard (or API) issues a link token → end user opens `/link/{token}` in a browser
- Picks an integration (options are filtered by the token's categories) → enters name → **Connect**
- Normalized sample data is synced immediately, the page hands the **public token** back, and the platform fires a signed `LinkedAccount.linked` webhook

### 3. Agent Handler (`/api/v1/...`)

Agents bootstrap themselves (no human, no OAuth) and a human later claims the org:

```bash
curl -X POST localhost:8000/api/v1/agent/signup/
# -> { "api_key": "cxa_…", "tool_pack_id": "…", "registered_user_id": "…",
#      "claim_link": "http://…/claim/<token>", "next_step": "Ask a human to open the claim link…" }
```

The human opens the claim link while logged into the console (`/claim/{token}`) to take ownership.

Then everything is MCP over JSON-RPC 2.0 (plain JSON or SSE framing if the client sends `Accept: text/event-stream`):

```bash
AK=cxa_your_agent_key
MCP=localhost:8000/api/v1/tool-packs/<pack_id>/registered-users/<user_id>/mcp/

curl $MCP -H "Authorization: Bearer $AK" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
curl $MCP -H "Authorization: Bearer $AK" -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
curl $MCP -H "Authorization: Bearer $AK" -d '{
  "jsonrpc":"2.0","id":3,"method":"tools/call",
  "params":{"name":"weather__get_forecast","arguments":{"input":{"latitude":38.9,"longitude":-77.0}}}}'
```

- **36 tools** out of the box (starter pack: `weather`, `wikipedia`, `jira`, `slack`, `notion`, `github`, `gmail`, `helpdesk` — 2 standard meta-tools per connector included)
- Tool inputs are wrapped as `{"input": {...}}` and validated against pydantic schemas → `invalid_arguments` errors carry the field-level detail
- Every connector exposes `authenticate_<slug>` + `<slug>__validate_credential`
- **Credential contract:** tools called without a connection return the machine-actionable `reauth_required` payload pointing at `authenticate_<slug>` — exactly the contract commercial platforms expose
- Tool discovery: `POST /api/v1/tool-packs/{pack}/registered-users/{user}/search/` with `{"intent": "…"}`; unmatched intents return `request_tool_access` guidance
- Security gateway: per-org entity rules (CREDIT_CARD with Luhn verification, EMAIL, SECRET_TOKEN, PHONE, IP, DATE_OF_BIRTH + custom regex) with `ALLOW / REDACT / BLOCK` actions, evaluated on input *and* output of every tool call
- Access requests: connectors outside the pack can be requested via `POST /api/v1/tool-access-requests/`, reviewed in the console
- Full audit log: `GET /api/v1/audit-log/`; metric usage: `credit_type per tool × call`

### 4. Gateway (`/v1/...`)

OpenAI-compatible model routing with consumption accounting:

```bash
GW=cxg_your_gateway_key
curl -H "Authorization: Bearer $GW" localhost:8000/v1/models
curl -H "Authorization: Bearer $GW" localhost:8000/v1/chat/completions \
     -d '{"messages":[{"role":"user","content":"Are you the router?"}]}'
# {"choices":[{"message":{"role":"assistant","content":"…"}}],
#  "usage":{"prompt_tokens":…,"completion_tokens":…},
#  "connexa":{"credits_charged":0.0021,"attempts":[{"model":"gpt-4o","ok":false},{…,"ok":true}]}}
```

- **Routing policies** (`/v1/routing-policies`): ordered model chain per org (`quality-first`: gpt-4o → claude → gpt-4o-mini → connexa-echo). Vendors that aren't configured fail over to the next model; **`connexa-echo`** (deterministic local model, zero credentials) is the always-available last resort, so the plane works with no vendor keys at all
- **Streaming**: `"stream": true` emits OpenAI-style `chat.completion.chunk` SSE frames ending in `data: [DONE]`
- **`POST /v1/responses`** — light Responses-style wrapper with `output_text`
- **Budgets**: monthly credit cap per org (`GET /v1/budget`); exceeded → HTTP 402 `budget_exceeded`. Credits are computed per model pricing (`credits/1k tokens`, editable in `/dashboard/gateway`) and written to the usage meter
- `GET /v1/usage` — cross-plane credit summary; `GET /v1/vendors` — vendor/family aggregation

---

## Webhooks

Every delivery is signed over the **raw body**:

```
X-Connexa-Signature: sha256=hex(HMAC-SHA256(secret, raw_body))
X-Connexa-Event: LinkedAccount.linked
```

Verify (Python):

```python
expected = hmac.new(secret.encode(), await request.body(), hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, signature.removeprefix("sha256="))
```

Events fire on link (`LinkedAccount.linked`), writes (`Ticket.synced`, `Contact.synced`, `Deal.synced`), sync diffs (`LinkedAccount.synced`), plus `Test.connected` from the console. Endpoints + delivery receipts (status codes) are managed in **Console → Webhooks**.

---

## Workforce governance (IdP / teams / skills / memory / DLP)

Point your IdP at **SCIM 2.0** (`/scim/v2/Users`, `/scim/v2/Groups`) with a bearer data key. Directory
users land as registered users; groups become **teams**. Membership in a team is what grants governance:

```bash
curl -X POST localhost:8000/scim/v2/Groups -H "Authorization: Bearer $DATA_KEY" -d '{
  "displayName": "Support", "externalId": "okta-123",
  "members": [{"value": "<scim-user-id>"}],
  "connexa_extensions": {"allowed_models": ["connexa-echo"], "monthly_credit_budget": 500}}'
```

Then the gateway **enforces** it on every call that presents the team header:

```bash
curl -X POST localhost:8000/v1/chat/completions -H "Authorization: Bearer $GATEWAY_KEY" \
  -H "X-Connexa-Team: Support" -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
# -> 403 model_not_allowed_for_team (echo is approved; gpt-4o-mini is not)
```

- **Per-team model allowlists** — unapproved models are refused; routing-policy failover chains are filtered to approved models only
- **Hard per-team budgets** — 402 `team_budget_exceeded` once a team's monthly credits are spent (checked before invocation, not merely logged)
- **DLP on outbound model traffic** — prompts are scanned with the same rule engine as agent tool traffic: REDACT rewrites before the model sees them, BLOCK refuses the request (403), every action audited
- **Shared skills library** — versioned playbooks at `/api/workforce/v1/skills`; every agent sees them via the `skills_list` MCP tool
- **Employee AI memory** — org-scoped context at `/api/workforce/v1/memories` (searchable); agents read/write via `memory_save` / `memory_search` MCP tools
- **IdP deprovisioning** — SCIM `replace active:false` deactivates the user everywhere
- Full teams/skills/memories management in **Console → Workforce**

### Data-plane governance (the Unified extras)

- **Scheduled background sync** — set per-account frequency (`POST .../accounts/{token}/sync-frequency` or Console → Linked Accounts); a boot-time asyncio loop re-pulls due accounts with the same sync engine
- **Selective sync + ignored remote objects** — `account.settings = {"selective_sync": {"Deal": false}, "ignored_remote_ids": [...]}` — excluded models/objects are never loaded
- **Field-level scopes** — a key's `field_scopes` (`{"Contact": {"exclude": ["email"]}}`) filter response fields (data minimization); list endpoints also accept `include_remote_data=false`
- **Issues surface** — `GET /api/{cat}/v1/issues` lists open sync issues; per-row sync errors are recorded, never silent
- **Erasure** — `DELETE /api/{cat}/v1/accounts/{token}` removes the account and every derived row (end-user deletion requests)

---

## Categories & Common Models

Six data categories ship in one process (the full public unified set), each with a normalized
Common Model contract (`GET /api/{cat}/v1/meta/write-schema` before writes):

| Category | Common Models | Starter connectors (data plane) |
|---|---|---|
| Ticketing | `Ticket` | helpdesk · **github-issues (REAL lane)** |
| CRM | `Contact`, `Deal` | hubspot, salespod |
| HRIS | `Employee` | peopleos |
| Accounting | `Invoice` | ledgerly |
| ATS | `Candidate` | hirepod |
| File Storage | `File`, `Folder` | vaultfile |

Every category gets the full data-plane surface automatically: link → initial sync →
list/get/create/patch → sync schedule → issues → erasure → field scopes.
A live, proof-backed feature registry (with roadmap items labeled honestly) is at **/features**.

## Field mappings (per account)

Provider payloads use provider names; mappings rename them onto the Common Model before
normalization — the same idea as Merge's field mappings, applied by the sync engine:

```bash
curl -X PUT localhost:8000/api/hris/v1/accounts/$TOKEN/field-mappings \
  -H "Authorization: Bearer $DATA_KEY" \
  -d '{"mappings": {"Employee": {"team": "department", "manager": "manager_name"}}}'
# -> mappings stored AND a re-sync runs immediately: department/manager_name populate

curl localhost:8000/api/hris/v1/accounts/$TOKEN/field-mappings/suggestions
# -> {"suggestions": {"Employee": {"unmapped_provider_fields": {"team": 8, ...},
#                                  "suggested_targets": {"team": "department"}}}}
```

- Invalid targets are rejected (must be real Common Model fields)
- Unmapped provider fields always keep flowing into `remote_data` — nothing is dropped
- Console → Linked Accounts → **Field mappings** editor (add/remove + auto re-sync)

## Webhook retries with backoff

A failed delivery is never silent: the scheduler re-drives retries with exponential
backoff (**30s → 2m → 10m**, 4 attempts total), recording every attempt on the delivery
row (Console → Webhooks shows attempts + next retry). Endpoint deleted → retries stop.

## Link flow customization

Link tokens accept theming shown on the end-user connect screen:

```bash
curl -X POST localhost:8000/api/link-tokens -H "Authorization: Bearer $DATA_KEY" -d '{
  "categories": ["hris"],
  "company_name": "Acme Corp", "accent_color": "#00a854",
  "logo_url": "https://acme.example/logo.png",
  "welcome_text": "Acme needs ERP access."}'
```

## DLP on both sides of the model

Request-side: prompts are scanned before dispatch — BLOCK refuses (403), REDACT rewrites.
Response-side: model output is scanned before it leaves the gateway — matches are
redacted and reported in the `connexa.dlp` envelope. Every action is audited.

---

## Self-serve SaaS (org accounts)

Every aspect of running the platform is usable without talking to an operator:

```bash
# 1) become a tenant — returns live data + gateway API keys (shown once)
curl -X POST localhost:8000/api/v1/org/signup \
     -d '{"email": "you@company.com", "password": "at-least-8-chars", "company_name": "Company"}'
# -> { "data_api_key": "cxk_…", "gateway_api_key": "cxg_…", "plan": "free", … }

# 2) use it: link tokens, syncs, tickets, gateway calls — all immediately live

# 3) come back later: password in, account view out (keys masked)
curl -X POST localhost:8000/api/v1/org/login \
     -d '{"email": "you@company.com", "password": "…"}'

# 4) account + billing surfaces (with your data key)
curl -H "Authorization: Bearer cxk_…" localhost:8000/api/v1/org/me
curl -H "Authorization: Bearer cxk_…" localhost:8000/api/v1/org/billing
```

- Passwords hashed with **scrypt** (per-user salt, constant-time verify)
- **Plans are enforced, not decorative:** `free` allows 3 linked accounts — the 4th connect gets `402 plan_limit_reached` with the plan and limit in the body. `growth` = 25, `scale` = 1M
- `/api/v1/org/billing` reports real usage against real limits and exposes `upgrade_url` when `CONNEXA_STRIPE_PAYMENT_LINK` is set (pay the link → billing webhook raises the plan); without it the API answers `payments_configured: false` — nothing pretends
- Agents get the same treatment at `POST /api/v1/agent/signup/` (machine tenants)

## A real data lane: GitHub Issues → Tickets

`github-issues` is the first **real** data-plane connector — no simulation:

```bash
curl -X POST localhost:8000/api/link-tokens -H "Authorization: Bearer $DATA_KEY" \
     -d '{"categories": ["ticketing"]}'
# open /link/<token> — GitHub Issues is listed with a LIVE badge

curl -X POST localhost:8000/link/<token>/connect \
     -d '{"integration": "github-issues", "end_user_name": "Eng",
          "credential": "ghp_your_pat", "config": {"repo": "owner/name"}}'

curl -X POST -H "Authorization: Bearer $DATA_KEY" \
     localhost:8000/api/ticketing/v1/accounts/<public_token>/sync
# -> real GitHub issues land as normalized Tickets (status, labels, assignee, body)
```

- The account's **own PAT** is used (or the platform `CONNEXA_GITHUB_TOKEN`); provider 401/404s become `SyncIssue` rows — **a failing real lane never fabricates data**
- Provider field names (`state`, `labels`) ride along for field mappings; PRs are excluded automatically
- Every simulated connector is badged `demo` on the link page — the honest/real boundary is always visible

---

## Connectors

| Slug | Lane | Credentials | Live data |
|---|---|---|---|
| `weather` | agent | none | **live** (Open-Meteo) |
| `wikipedia` | agent | none | **live** (Wikipedia) |
| `jira` | agent | synthetic (demo) | simulated backlog |
| `helpdesk` | link ticketing + agent | synthetic | simulated tickets |
| `hubspot` | link crm + agent | synthetic | simulated CRM |
| `salespod` | link crm | synthetic | simulated CRM |
| `slack` | agent | `CONNEXA_SLACK_TOKEN` or OAuth / pasted bot token | live sends/channel lists |
| `notion` | agent | `CONNEXA_NOTION_TOKEN` or OAuth / pasted token | live search/page create |
| `github` | agent | `CONNEXA_GITHUB_TOKEN` or OAuth / pasted PAT | live issues/repo data |
| `github-issues` | **link ticketing (REAL data lane)** | per-account PAT (link flow) or `CONNEXA_GITHUB_TOKEN` | **real repo issues → Ticket rows** |
| `gmail` | agent | `CONNEXA_GMAIL_TOKEN` or Google OAuth / pasted token | live mailbox search + real sends |

**Going live with real providers:** copy `.env.example` → `.env`, paste any of

```
CONNEXA_SLACK_TOKEN=xoxb-…     # Bot User OAuth Token (chat:write, channels:read)
CONNEXA_NOTION_TOKEN=ntn_…     # internal integration token
CONNEXA_GITHUB_TOKEN=ghp_…     # PAT with repo scope
```

and restart. Those connectors flip to `real: true` (live lane) automatically; without tokens they run the demo lane so every code path stays exercisable. `authenticate_<slug>` inserts demo credentials so agents can test the full loop offline. Provider LLM keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) light up the gateway vendors the same way.

## OAuth "social login" + paste-token (per-user real accounts)

Beyond platform-wide env tokens, registered users can hold **per-account credentials** in three ways (priority order used for every tool call: OAuth grant → pasted token → platform token → demo):

1. **Full OAuth 2.0 dance** — register a free app at the provider, paste its client id/secret into `.env`:

   ```
   CONNEXA_REDIRECT_BASE=http://localhost:8000
   CONNEXA_SLACK_CLIENT_ID=…        CONNEXA_SLACK_CLIENT_SECRET=…
   CONNEXA_NOTION_CLIENT_ID=…       CONNEXA_NOTION_CLIENT_SECRET=…
   CONNEXA_GITHUB_OAUTH_CLIENT_ID=… CONNEXA_GITHUB_OAUTH_CLIENT_SECRET=…
   CONNEXA_GMAIL_CLIENT_ID=…        CONNEXA_GMAIL_CLIENT_SECRET=…
   ```

   Then `authenticate_<slug>` (MCP) and `GET /api/v1/registered-users/{ru}/oauth/{slug}/start` (REST) return a real authorization URL bound to that user with an HMAC-signed, 15-minute state. The human approves in the browser; the provider 302s to `GET /oauth/{slug}/callback`, Connexa exchanges the code server-side (Slack `oauth.v2.access`, Notion `Basic`-auth token endpoint, GitHub standard form) and stores `{"type":"oauth", access_token, account_label}` on the user. Subsequent tool calls run live automatically; expiring tokens refresh via `maybe_refresh` when a refresh token is issued.

2. **Paste a token you already have** — instant live account, no app registration:

   ```
   POST /api/v1/registered-users/{registered_user_id}/credentials/
   Authorization: Bearer cxa_…
   {"connector": "github", "token": "ghp_…"}
   ```

   Connexa probes the provider for a human label (workspace name / login) when reachable and stores it; tool calls hit the real provider from then on.

3. **Platform token** — the `.env` lane above, shared by all users.

Both user-bound lanes and every failure path (tampered state, provider `access_denied`, exchange failure, unverified token) render/return gracefully and are audited (`AGENT_OAUTH_CONNECTED`, `AGENT_OAUTH_FAILED`, `AGENT_CREDENTIAL_PASTED`). The offline mock provider (`mock_oauth.py` in the test rig) proves the whole dance end-to-end without any vendor account.

Adding a connector = one class implementing the `Connector` interface (`connexa/connectors/base.py`): `build_tools()` returning `ToolSpec`s, optional `generate_sample(category)` to back the data plane, optional `passthrough()`, optional `configure_oauth(...)`/`exchange_code()`/`describe_token()` for the social-login lane. Register it in `connectors/registry.py` — done: it appears in the catalog, tool packs, search, security gateway, and metering.

**Connector SDK:** plain REST providers don't need a class at all — declare each tool as a `RestCall` and instantiate `SimpleRestConnector` (`connexa/connectors/sdk.py`). Full guide with copy-paste templates: **[`docs/connector-sdk.md`](docs/connector-sdk.md)**.

---

## Admin console

Server-rendered (Jinja2, zero npm):

- **Overview** — counts for linked accounts, tickets, CRM rows, agent orgs, live usage meter
- **Linked Accounts** — Merge-style account table with per-model row counts, last-sync stamp, and a **Sync now** button (runs the same sync engine as the API)
- **Link & Data** — create link tokens (shows the URL), inspect linked accounts
- **API Keys** — create/revoke data, gateway, and admin keys (created key shown once)
- **Webhooks** — endpoints, delivery receipts with HTTP statuses
- **Audit Trail** — full platform accountability stream (keys, links, syncs, approvals)
- **Agent Handler** — agent orgs + claim state, tool packs + connectors, tool-access review queue, full audit stream
- **Gateway** — model catalog with pricing, routing policies (make default), monthly budget, gateway usage

Login: `/login` with the admin token (`dev-admin` by default). Cookie-authenticated, standard browsers work out of the box.

---

## Accounting model

| Plane | Metered | Ledger |
|---|---|---|
| Agent | each tool call: credit_type (low/medium/high) | `usage_logs` + audit |
| Gateway | tokens × model pricing (input/output) | `usage_logs` + budget decrement |
| Data | writes/passthrough sync ops | `usage_logs` |
| Link | initial sync | `usage_logs` |

`usage_logs` counts **credits**, not dollars — swap the weights for real pricing.

---

## Project layout

```
connexa/
  connexa/
    main.py              # app assembly + startup seeding + boot console
    config.py db.py models.py security.py metering.py utils.py
    common_models.py     # per-category Common Model + write-schema builder
    unified.py           # plane 1: data plane
    link.py org_auth.py seed_data.py  # plane 2: link flow + self-serve SaaS + initial sync
    events.py            # webhook bus (HMAC raw-body, delivery receipts)
    agent/
      handler.py         # plane 3: agent REST (signup/packs/users/keys/rules/audit/search)
      mcp.py             # MCP JSON-RPC + security gateway executor
    connectors/
      base.py sdk.py registry.py   # Connector interface, declarative SDK, registry
      free_live.py       # weather + wikipedia (no-cred live)
      simulated.py       # jira/hubspot/helpdesk/salespod demo lane (badged `demo`)
      real_slack.py real_notion.py real_github.py real_gmail.py
      real_github_issues.py  # REAL ticketing data lane (GitHub issues -> Ticket)
    gateway/
      providers.py       # echo + openai + anthropic adapters
      api.py             # plane 4: chat/stream/policies/budgets
    dashboard/           # server-rendered console (routes + templates)
    seed.py
  docs/
    connector-sdk.md     # add-a-connector guide (declarative + full-class recipes)
  connector-sdk.md       # add-a-connector guide (declarative + full-class recipes)
  run.py requirements.txt .env.example Dockerfile docker-compose.yml DEPLOY.md
  connexa.db             # SQLite (delete to re-seed)
```

Original code throughout — the platform reimplements *architectural patterns* (unified common models, link tokens, agent tool servers, model routers); no proprietary code or assets are used. Not affiliated with any commercial unified-API vendor.

## Roadmap candidates

- Grow the live connector set (Calendar, Drive, HubSpot, Jira, Stripe …) — mostly `RestCall` declarations now that the SDK exists
- More categories' real lanes (ATS, File Storage real providers) — the Common Model + adapter pattern is proven by HRIS/Accounting and the REAL github-issues lane
- Per-registered-user credential vault; prebuilt Okta / Entra SCIM connectors
- Streaming-response DLP (non-streaming responses redacted today)
- Cross-process job queue for multi-worker deployments
