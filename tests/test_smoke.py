"""End-to-end smoke: boots the whole platform (all four planes) on a throwaway DB.

Run with:  python -m pytest -q
"""
import json
import os
import tempfile

# point the platform at a disposable DB *before* importing connexa (config reads env at import)
_TEST_DB = os.path.join(tempfile.gettempdir(), "connexa-smoke.db")
try:
    if os.path.exists(_TEST_DB):
        os.remove(_TEST_DB)          # a previous run's lock may linger on Windows — reuse then
except PermissionError:
    pass
os.environ["CONNEXA_DB"] = _TEST_DB

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from connexa.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["service"] == "connexa"


def test_agent_signup_self_serves(client):
    response = client.post("/api/v1/agent/signup/")
    assert response.status_code == 201
    body = response.json()
    for field in ("api_key", "tool_pack_id", "registered_user_id", "claim_link"):
        assert body[field], f"signup must return {field}"
    assert body["api_key"].startswith("cxa_")


def test_starter_pack_tools_and_gmail_demo_lane(client):
    signup = client.post("/api/v1/agent/signup/").json()
    key, pack, user = signup["api_key"], signup["tool_pack_id"], signup["registered_user_id"]
    headers = {"Authorization": f"Bearer {key}"}
    url = f"/api/v1/tool-packs/{pack}/registered-users/{user}/mcp/"

    def call(name, arguments=None):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": {"input": arguments or {}}}}
        response = client.post(url, json=payload, headers=headers)
        assert response.status_code == 200
        result = response.json()["result"]
        return json.loads(result["content"][0]["text"])

    # the pack exposes every starter connector including the meta tools
    listing_resp = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                               headers=headers)
    listing = listing_resp.json()
    assert listing_resp.status_code == 200 and "result" in listing, listing_resp.text[:400]
    names = {tool["name"] for tool in listing["result"]["tools"]}
    for expected in ("gmail__send_email", "gmail__validate_credential", "authenticate_gmail",
                     "slack__list_channels", "notion__search", "github__get_repository"):
        assert expected in names, f"{expected} missing from starter pack"

    # unconnected -> machine-actionable reauth contract
    blocked = call("gmail__validate_credential")
    assert blocked["error_reason"] == "reauth_required"
    assert blocked["data"]["authenticate_tool"] == "authenticate_gmail"

    # authenticate -> demo credentials -> demo lane answers
    connected = call("authenticate_gmail")
    assert connected["status"] == "connected"
    assert call("gmail__validate_credential")["status"] == "validated"
    sent = call("gmail__send_email", {"to": "someone@example.com", "subject": "Hi", "body": "demo"})
    assert sent["mode"] == "demo" and sent["sent"] is True


# ---------------------------------------------------------------- public site

def test_marketing_site_pages(client):
    landing = client.get("/")
    assert landing.status_code == 200
    assert "Connexa" in landing.text and "Agent Handler" in landing.text
    for path in ("/unified", "/agent-handler", "/gateway", "/workforce",
                 "/pricing", "/security", "/integrations"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} must render"
    assert "Common Model" in client.get("/unified").text
    assert "SOC 2" in client.get("/security").text          # honest roadmap, present in copy


def test_dashboard_accounts_and_audit_require_login(client):
    assert client.get("/dashboard/accounts").url.path == "/login"
    assert client.get("/dashboard/audit").url.path == "/login"


# ---------------------------------------------------------------- sync engine

def test_sync_engine_idempotent_restores_rows(client):
    """A linked account syncs to Common Model rows; deleting rows then re-syncing
    restores them, and a second sync reports 'unchanged' (the Merge-style contract)."""
    from connexa.db import SessionLocal
    from connexa.models import ApiKey, LinkedAccount, Ticket

    db = SessionLocal()
    try:
        key = (db.query(ApiKey).filter(ApiKey.scope == "data", ApiKey.is_test == True)  # noqa: E712
               .order_by(ApiKey.created_at).first().key_secret)
    finally:
        db.close()
    headers = {"Authorization": f"Bearer {key}"}

    token = client.post("/api/link-tokens", json={"categories": ["ticketing"]},
                        headers=headers).json()["link_token"]
    connected = client.post(f"/link/{token}/connect",
                            json={"integration": "helpdesk", "end_user_name": "Sync Tester"})
    assert connected.status_code == 200
    public_token = connected.json()["public_token"]

    def sync():
        response = client.post(f"/api/ticketing/v1/accounts/{public_token}/sync", headers=headers)
        assert response.status_code == 200
        return response.json()

    first = sync()
    assert first["integration"] == "helpdesk" and first["unchanged"] >= 1

    # wipe the normalized rows — the sync must restore them (not just the first run)
    db = SessionLocal()
    try:
        account = db.query(LinkedAccount).filter(LinkedAccount.account_token == public_token).first()
        wiped = (db.query(Ticket).filter(Ticket.linked_account_id == account.id)
                 .delete())
    finally:
        db.commit()
        db.close()
    assert wiped >= 1

    restored = sync()
    assert restored["added"] == first["unchanged"], "sync must restore wiped rows"
    assert restored["counts"]["Ticket"] == first["unchanged"]

    again = sync()
    assert again["added"] == 0 and again["updated"] == 0 and again["unchanged"] >= 1, "idempotent"


# ---------------------------------------------------------------- workforce governance

def _workforce_key(client) -> str:
    from connexa.db import SessionLocal
    from connexa.models import ApiKey

    db = SessionLocal()
    try:
        return (db.query(ApiKey).filter(ApiKey.scope == "data", ApiKey.is_test == True)  # noqa: E712
                .order_by(ApiKey.created_at).first().key_secret)
    finally:
        db.close()


def _gateway_key(client) -> str:
    from connexa.db import SessionLocal
    from connexa.models import ApiKey

    db = SessionLocal()
    try:
        return (db.query(ApiKey).filter(ApiKey.scope == "gateway")
                .order_by(ApiKey.created_at).first().key_secret)
    finally:
        db.close()


def test_scim_provisioning_team_allowlist_and_budget(client):
    """IdP -> SCIM user + group -> team; gateway enforces allowlist and budget."""
    headers = {"Authorization": f"Bearer {_workforce_key(client)}"}

    user = client.post("/scim/v2/Users", headers=headers, json={
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": "dana@acme.test", "name": {"formatted": "Dana Reed"}, "externalId": "idp-1"})
    assert user.status_code == 201 and user.json()["active"] is True
    user_id = user.json()["id"]

    group = client.post("/scim/v2/Groups", headers=headers, json={
        "displayName": "Support", "externalId": "grp-9", "members": [{"value": user_id}],
        "connexa_extensions": {"allowed_models": ["connexa-echo"], "monthly_credit_budget": 5000}})
    assert group.status_code == 201
    team_id = group.json()["id"]
    assert [m["value"] for m in group.json()["members"]] == [user_id]

    teams = client.get("/api/workforce/v1/teams", headers=headers).json()
    team = next(t for t in teams["teams"] if t["id"] == team_id)
    assert team["allowed_models"] == ["connexa-echo"] and team["monthly_credit_budget"] == 5000
    assert team["members"] and team["members"][0]["user_id"] == user_id

    # allowlist enforcement: unapproved model refused, approved model passes
    gateway_headers = {"Authorization": f"Bearer {_gateway_key(client)}"}
    blocked = client.post("/v1/chat/completions", headers={**gateway_headers, "X-Connexa-Team": "Support"},
                          json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["error"]["code"] == "model_not_allowed_for_team"

    ok = client.post("/v1/chat/completions", headers={**gateway_headers, "X-Connexa-Team": "Support"},
                     json={"model": "connexa-echo",
                           "messages": [{"role": "user", "content": "say hi"}]})
    assert ok.status_code == 200 and ok.json()["connexa"]["team"] == "Support"

    # hard team budget: exhaust it, next call refused with 402
    patched = client.patch(f"/api/workforce/v1/teams/{team_id}", headers=headers,
                           json={"monthly_credit_budget": 0})
    assert patched.status_code == 200
    broke = client.post("/v1/chat/completions", headers={**gateway_headers, "X-Connexa-Team": "Support"},
                        json={"model": "connexa-echo", "messages": [{"role": "user", "content": "hi"}]})
    assert broke.status_code == 402
    assert broke.json()["detail"]["error"]["code"] == "team_budget_exceeded"

    # IdP deprovisioning deactivates the user
    dep = client.patch(f"/scim/v2/Users/{user_id}", headers=headers, json={
        "Operations": [{"op": "replace", "value": {"active": False}}]})
    assert dep.status_code == 200 and dep.json()["active"] is False


def test_skills_and_memories_rest_and_mcp(client):
    headers = {"Authorization": f"Bearer {_workforce_key(client)}"}

    skill = client.post("/api/workforce/v1/skills", headers=headers, json={
        "name": "escalation-playbook", "instructions": "1) find ticket 2) ping owner",
        "tags": ["support"]})
    assert skill.status_code == 201
    listed = client.get("/api/workforce/v1/skills", headers=headers).json()
    assert listed["count"] >= 1 and any(s["name"] == "escalation-playbook" for s in listed["skills"])

    memory = client.post("/api/workforce/v1/memories", headers=headers, json={
        "content": "Acme escalations go to Dana", "tags": ["support"]})
    assert memory.status_code == 201
    found = client.get("/api/workforce/v1/memories", headers=headers, params={"q": "Dana"}).json()
    assert found["count"] >= 1 and "Dana" in found["memories"][0]["content"]

    # agents reach both through MCP builtins
    signup = client.post("/api/v1/agent/signup/").json()
    mcp_headers = {"Authorization": f"Bearer {signup['api_key']}"}
    mcp_url = (f"/api/v1/tool-packs/{signup['tool_pack_id']}"
               f"/registered-users/{signup['registered_user_id']}/mcp/")

    def mcp(method, params):
        response = client.post(mcp_url, headers=mcp_headers,
                               json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        return response.json()["result"]

    tools = mcp("tools/list", {})["tools"]
    names = {t["name"] for t in tools}
    assert {"memory_save", "memory_search", "skills_list"} <= names

    saved = json.loads(mcp("tools/call", {"name": "memory_save",
                                          "arguments": {"content": "QA rotation weekly on Mondays"}}
                           )["content"][0]["text"])
    assert saved["saved"] is True
    found = json.loads(mcp("tools/call", {"name": "memory_search",
                                          "arguments": {"query": "rotation"}})["content"][0]["text"])
    assert found["count"] >= 1

    # skills are org-scoped: publish one in the agent's own org, then list via MCP
    from connexa.db import SessionLocal
    from connexa.models import ApiKey, Org, Skill
    from connexa.utils import uid as _uid

    db = SessionLocal()
    try:
        agent_org_id = db.query(ApiKey).filter(
            ApiKey.key_hash == __import__("hashlib").sha256(signup["api_key"].encode()).hexdigest()
        ).first().org_id
        db.add(Skill(id=_uid(), org_id=agent_org_id, name="triage-playbook",
                     instructions="1) read ticket 2) label 3) route", tags=["support"]))
        db.commit()
    finally:
        db.close()

    skills = json.loads(mcp("tools/call", {"name": "skills_list", "arguments": {}})["content"][0]["text"])
    assert skills["count"] >= 1 and skills["skills"][0]["instructions"]


def test_dlp_on_outbound_model_traffic(client):
    """Gateway prompts are scanned: REDACT rewrites before the model sees it, BLOCK refuses."""
    from connexa.db import SessionLocal
    from connexa.models import ApiKey, SecurityRule
    from connexa.utils import uid

    gateway_headers = {"Authorization": f"Bearer {_gateway_key(client)}"}

    db = SessionLocal()
    try:
        org_id = db.query(ApiKey).filter(ApiKey.scope == "gateway").first().org_id
        db.add(SecurityRule(id=uid(), agent_org_id=org_id, entity_type="CUSTOM",
                            name="no-acme", pattern=r"Acme Corp", action="BLOCK", scope="org",
                            is_active=True))
        db.add(SecurityRule(id=uid(), agent_org_id=org_id, entity_type="EMAIL",
                            name="emails", pattern=r"[\w.+-]+@[\w-]+\.[\w.]+", action="REDACT",
                            scope="org", is_active=True))
        db.commit()
    finally:
        db.close()

    blocked = client.post("/v1/chat/completions", headers=gateway_headers, json={
        "messages": [{"role": "user", "content": "Summarize the Acme Corp renewal thread"}]})
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["error"]["code"] == "dlp_blocked"

    # the EMAIL rule REDACTs before the model sees the prompt
    sent = client.post("/v1/chat/completions", headers=gateway_headers, json={
        "messages": [{"role": "user", "content": "Reply OK. Contact is dana@acme.test"}]}).json()
    assert sent["connexa"]["dlp"], "redaction must be reported in the response envelope"


def test_scheduled_sync_tick(client):
    """The background scheduler syncs accounts whose frequency elapsed — and only those."""
    from datetime import timedelta

    from connexa.db import SessionLocal
    from connexa.models import ApiKey, LinkedAccount
    from connexa.scheduler import tick
    from connexa.utils import naive_now

    headers = {"Authorization": f"Bearer {_workforce_key(client)}"}
    token = client.post("/api/link-tokens", json={"categories": ["ticketing"]},
                        headers=headers).json()["link_token"]
    public_token = client.post(f"/link/{token}/connect",
                               json={"integration": "helpdesk", "end_user_name": "Sched"}).json()["public_token"]

    freq = client.post(f"/api/ticketing/v1/accounts/{public_token}/sync-frequency",
                       headers=headers, json={"seconds": 300})
    assert freq.status_code == 200 and freq.json()["scheduled"] is True

    db = SessionLocal()
    try:
        account = db.query(LinkedAccount).filter(LinkedAccount.account_token == public_token).first()
        account.last_synced_at = naive_now() - timedelta(hours=2)
        manual = db.query(LinkedAccount).filter(LinkedAccount.sync_frequency_seconds == 0).first()
        manual_before = manual.last_synced_at if manual else None
        db.commit()
    finally:
        db.close()

    from connexa.db import SessionLocal as SL
    session = SL()
    try:
        summaries = tick(session)
    finally:
        session.close()
    assert any(s["integration"] == "helpdesk" for s in summaries)

    db = SessionLocal()
    try:
        account = db.query(LinkedAccount).filter(LinkedAccount.account_token == public_token).first()
        assert (account.last_synced_at - naive_now()).total_seconds() < 120
        manual = db.query(LinkedAccount).filter(LinkedAccount.sync_frequency_seconds == 0).first()
        if manual and manual_before:
            assert manual.last_synced_at == manual_before
    finally:
        db.close()


def test_selective_sync_field_scopes_issues_erasure(client):
    from connexa.db import SessionLocal
    from connexa.models import ApiKey, Deal, LinkedAccount, Ticket
    from connexa.security import create_api_key
    from connexa.utils import uid as _uid

    headers = {"Authorization": f"Bearer {_workforce_key(client)}"}

    token = client.post("/api/link-tokens", json={"categories": ["crm"]},
                        headers=headers).json()["link_token"]
    public_token = client.post(f"/link/{token}/connect",
                               json={"integration": "hubspot", "end_user_name": "Gov"}).json()["public_token"]

    # ---- selective sync: Deals excluded, one remote object ignored ----
    db = SessionLocal()
    try:
        account = db.query(LinkedAccount).filter(LinkedAccount.account_token == public_token).first()
        ignored_remote = db.query(Deal).filter(Deal.linked_account_id == account.id).first().remote_id
        account.settings = {"selective_sync": {"Deal": False}, "ignored_remote_ids": [ignored_remote]}
        db.commit()
        wiped_deals = db.query(Deal).filter(Deal.linked_account_id == account.id).delete()
        db.commit()
    finally:
        db.close()
    assert wiped_deals >= 1

    summary = client.post(f"/api/crm/v1/accounts/{public_token}/sync", headers=headers).json()
    assert summary["skipped"] >= 1, "ignored/selected-out rows must be skipped, not loaded"
    deals_after = client.get("/api/crm/v1/deals", headers={**headers, "X-Account-Token": public_token}).json()
    assert deals_after["count"] == 0, "Deals are selectively sync'd off"
    contacts_after = client.get("/api/crm/v1/contacts", headers={**headers, "X-Account-Token": public_token}).json()
    assert contacts_after["count"] >= 1, "Contacts still sync"

    # ---- field-level scopes: a key that may not see emails ----
    db = SessionLocal()
    try:
        account_row = db.query(LinkedAccount).filter(LinkedAccount.account_token == public_token).first()
        from connexa.models import Org as _Org
        org = db.query(_Org).filter(_Org.id == account_row.org_id).first()
        scoped = create_api_key(db, org=org, name="minimized", scope="data", is_test=True)
        scoped.field_scopes = {"Contact": {"exclude": ["email", "remote_data"]}}
        db.commit()
        scoped_secret = scoped.key_secret
    finally:
        db.close()

    scoped_headers = {"Authorization": f"Bearer {scoped_secret}", "X-Account-Token": public_token}
    row = client.get("/api/crm/v1/contacts", headers=scoped_headers).json()["results"][0]
    assert "email" not in row and "remote_data" not in row and "first_name" in row

    # ---- issues surface ----
    issues = client.get("/api/crm/v1/issues", headers=headers).json()
    assert issues["status"] == "open" and isinstance(issues["issues"], list)

    # ---- erasure: account + every derived row gone ----
    erased = client.delete(f"/api/crm/v1/accounts/{public_token}", headers=headers)
    assert erased.status_code == 200 and erased.json()["erased"] is True
    after = client.get("/api/crm/v1/contacts", headers={**headers, "X-Account-Token": public_token})
    assert after.status_code == 404, "account token must die with the account"
