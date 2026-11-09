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
        os.remove(_TEST_DB)          # a previous run's lock may linger on Windows â€” reuse then
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
                 "/pricing", "/security", "/integrations", "/features"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} must render"
    assert "Common Model" in client.get("/unified").text
    assert "SOC 2" in client.get("/security").text          # honest roadmap, present in copy
    features = client.get("/features").text
    assert "Field mappings" in features and "roadmap" in features      # honest registry renders


def test_ats_and_filestorage_categories(client):
    """New categories: link -> initial sync -> list/get/create/patch on Candidate,
    File, Folder — plus a real field-mapping renames applied by the sync engine."""
    headers = {"Authorization": f"Bearer {_workforce_key(client)}"}

    token = client.post("/api/link-tokens", json={"categories": ["ats", "filestorage"]},
                        headers=headers).json()["link_token"]
    pt_ats = client.post(f"/link/{token}/connect",
                         json={"integration": "hirepod", "end_user_name": "Recruiting"}
                         ).json()["public_token"]
    token2 = client.post("/api/link-tokens", json={"categories": ["filestorage"]},
                         headers=headers).json()["link_token"]
    pt_files = client.post(f"/link/{token2}/connect",
                           json={"integration": "vaultfile", "end_user_name": "Docs"}).json()["public_token"]

    # --- ATS: synced candidates -------------------------------------------------
    candidates = client.get("/api/ats/v1/candidates",
                            headers={**headers, "X-Account-Token": pt_ats}).json()
    assert candidates["count"] >= 5 and candidates["results"][0]["first_name"]
    cand_id = candidates["results"][0]["id"]
    one = client.get(f"/api/ats/v1/candidates/{cand_id}",
                     headers={**headers, "X-Account-Token": pt_ats}).json()
    assert one["id"] == cand_id and one["remote_data"]

    created = client.post("/api/ats/v1/candidates",
                          headers={**headers, "X-Account-Token": pt_ats},
                          json={"first_name": "Ana", "last_name": "Moreno",
                                "application_status": "screening", "tags": ["python"]})
    assert created.status_code == 201 and created.json()["application_status"] == "screening"

    patched = client.patch(f"/api/ats/v1/candidates/{created.json()['id']}",
                           headers={**headers, "X-Account-Token": pt_ats},
                           json={"application_status": "offer"})
    assert patched.status_code == 200 and patched.json()["application_status"] == "offer"

    bad = client.post("/api/ats/v1/candidates", headers={**headers, "X-Account-Token": pt_ats},
                      json={"first_name": "Ana", "application_status": "gig"})
    assert bad.status_code == 400, "enum values validated on write"

    schema = client.get("/api/ats/v1/meta/write-schema", headers=headers).json()
    assert schema["Candidate"]["fields"]["application_status"]["values"][0] == "applied"

    # --- ATS field mappings: provider names -> Common Model (engine re-applies) --
    mapped = client.put(f"/api/ats/v1/accounts/{pt_ats}/field-mappings", headers=headers, json={
        "mappings": {"Candidate": {"job_title": "role", "office": "location",
                                   "stage": "application_status"}}})
    assert mapped.status_code == 200
    after = client.get("/api/ats/v1/candidates",
                       headers={**headers, "X-Account-Token": pt_ats}).json()
    roles = {r["role"] for r in after["results"]}
    assert "Senior Backend Engineer" in roles
    statuses = {r["application_status"] for r in after["results"]}
    assert "interview" in statuses and "hired" in statuses

    # --- File Storage: folders + files -------------------------------------------
    files = client.get("/api/filestorage/v1/files",
                       headers={**headers, "X-Account-Token": pt_files}).json()
    assert files["count"] >= 3
    names = {r["name"] for r in files["results"]}
    assert "architecture.md" in names
    assert files["results"][0]["remote_data"]

    folders = client.get("/api/filestorage/v1/folders",
                         headers={**headers, "X-Account-Token": pt_files}).json()
    assert folders["count"] >= 2
    assert {f["name"] for f in folders["results"]} >= {"Engineering", "Specs"}

    created_file = client.post("/api/filestorage/v1/files",
                               headers={**headers, "X-Account-Token": pt_files},
                               json={"name": "runbook.md", "mime_type": "text/markdown",
                                     "folder": "/Engineering", "tags": ["ops"]})
    assert created_file.status_code == 201 and created_file.json()["name"] == "runbook.md"

    schema_fs = client.get("/api/filestorage/v1/meta/write-schema", headers=headers).json()
    assert schema_fs["File"]["fields"]["name"]["required_on_create"] is True
    assert "size_bytes" in schema_fs["File"]["read_only"]


def test_audit_log_export_csv(client):
    """Org-scoped audit trail exports as downloadable CSV (compliance artifact)."""
    signup = client.post("/api/v1/agent/signup/").json()
    headers = {"Authorization": f"Bearer {signup['api_key']}"}

    export = client.get("/api/v1/audit-log/export", headers=headers)
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    lines = export.text.splitlines()
    assert lines[0] == "ts,event_type,actor,ip,description"
    assert len(lines) >= 2, "signup itself must have audited at least one event"
    assert "AGENT_SIGNUP" in export.text


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

    # wipe the normalized rows â€” the sync must restore them (not just the first run)
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
    """The background scheduler syncs accounts whose frequency elapsed â€” and only those."""
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


# ---------------------------------------------------------------- v0.4: categories + mappings + retries

def test_hris_and_accounting_categories(client):
    """New categories: link -> initial sync -> list/get/create on Employee and Invoice."""
    headers = {"Authorization": f"Bearer {_workforce_key(client)}"}

    token = client.post("/api/link-tokens", json={"categories": ["hris", "accounting"]},
                        headers=headers).json()["link_token"]
    pt_hris = client.post(f"/link/{token}/connect",
                          json={"integration": "peopleos", "end_user_name": "HR"}).json()["public_token"]
    token2 = client.post("/api/link-tokens", json={"categories": ["accounting"]},
                         headers=headers).json()["link_token"]
    pt_acc = client.post(f"/link/{token2}/connect",
                         json={"integration": "ledgerly", "end_user_name": "Fin"}).json()["public_token"]

    employees = client.get("/api/hris/v1/employees", headers={**headers, "X-Account-Token": pt_hris}).json()
    assert employees["count"] >= 5 and employees["results"][0]["first_name"]
    emp_id = employees["results"][0]["id"]
    one = client.get(f"/api/hris/v1/employees/{emp_id}",
                     headers={**headers, "X-Account-Token": pt_hris}).json()
    assert one["id"] == emp_id

    created = client.post("/api/hris/v1/employees", headers={**headers, "X-Account-Token": pt_hris},
                          json={"first_name": "Ana", "last_name": "Moreno",
                                "email": "ana@acme.example", "employment_type": "contractor"})
    assert created.status_code == 201 and created.json()["employment_type"] == "contractor"

    bad = client.post("/api/hris/v1/employees", headers={**headers, "X-Account-Token": pt_hris},
                      json={"first_name": "Ana", "employment_type": "gig"})
    assert bad.status_code == 400, "enum values validated on write"

    invoices = client.get("/api/accounting/v1/invoices",
                          headers={**headers, "X-Account-Token": pt_acc}).json()
    assert invoices["count"] >= 5
    created_inv = client.post("/api/accounting/v1/invoices",
                              headers={**headers, "X-Account-Token": pt_acc},
                              json={"vendor_name": "Acme Supplies", "total_amount": 99.5,
                                    "status": "submitted"})
    assert created_inv.status_code == 201 and created_inv.json()["vendor_name"] == "Acme Supplies"

    schema = client.get("/api/accounting/v1/meta/write-schema", headers=headers).json()
    assert schema["Invoice"]["fields"]["status"]["values"][0] == "draft"


def test_field_mappings_engine_and_suggestions(client):
    """Provider fields (team/manager/vendor/total) map onto Common Model fields; the
    sync engine applies mappings and re-syncs; suggestions surface unmapped fields."""
    headers = {"Authorization": f"Bearer {_workforce_key(client)}"}

    token = client.post("/api/link-tokens", json={"categories": ["hris"]},
                        headers=headers).json()["link_token"]
    pt = client.post(f"/link/{token}/connect",
                     json={"integration": "peopleos", "end_user_name": "HR"}).json()["public_token"]

    # before mappings: provider's 'team' never lands in department (stays in remote_data)
    before = client.get("/api/hris/v1/employees", headers={**headers, "X-Account-Token": pt}).json()
    assert before["count"] >= 5
    assert all(r["department"] is None for r in before["results"])

    # suggestions: 'team' is an unmapped provider field observed in payloads
    suggestions = client.get(f"/api/hris/v1/accounts/{pt}/field-mappings/suggestions",
                             headers=headers).json()
    assert "team" in suggestions["suggestions"]["Employee"]["unmapped_provider_fields"]

    # set mappings -> engine re-syncs -> department populated from provider 'team'
    mapped = client.put(f"/api/hris/v1/accounts/{pt}/field-mappings", headers=headers, json={
        "mappings": {"Employee": {"team": "department", "manager": "manager_name"}}})
    assert mapped.status_code == 200
    assert mapped.json()["reapplied_sync"]["updated"] >= 1

    after = client.get("/api/hris/v1/employees", headers={**headers, "X-Account-Token": pt}).json()
    departments = {r["department"] for r in after["results"]}
    assert "Engineering" in departments and "Finance" in departments
    managers = {r["manager_name"] for r in after["results"]}
    assert "Priya Nair" in managers

    # invalid target rejected
    invalid = client.put(f"/api/hris/v1/accounts/{pt}/field-mappings", headers=headers, json={
        "mappings": {"Employee": {"team": "nope_field"}}})
    assert invalid.status_code == 400

    # same engine on accounting: vendor -> vendor_name, total -> total_amount
    token2 = client.post("/api/link-tokens", json={"categories": ["accounting"]},
                         headers=headers).json()["link_token"]
    pt2 = client.post(f"/link/{token2}/connect",
                      json={"integration": "ledgerly", "end_user_name": "Fin"}).json()["public_token"]
    client.put(f"/api/accounting/v1/accounts/{pt2}/field-mappings", headers=headers, json={
        "mappings": {"Invoice": {"vendor": "vendor_name", "total": "total_amount",
                                 "issued": "issue_date", "due": "due_date"}}})
    inv = client.get("/api/accounting/v1/invoices", headers={**headers, "X-Account-Token": pt2}).json()
    vendors = {r["vendor_name"] for r in inv["results"]}
    assert "Northwind Ltd" in vendors
    totals = [r["total_amount"] for r in inv["results"]]
    assert any(t and t > 0 for t in totals)


def test_webhook_retries_with_backoff(client):
    """A dead endpoint's deliveries retry with growing backoff and go dead (no next
    retry scheduled) after the final attempt."""
    from datetime import timedelta

    from connexa.db import SessionLocal
    from connexa.events import MAX_ATTEMPTS, attempt_delivery, due_retries, emit
    from connexa.models import ApiKey, WebhookDelivery, WebhookEndpoint
    from connexa.utils import naive_now as _now
    from connexa.utils import uid as _uid

    headers = {"Authorization": f"Bearer {_workforce_key(client)}"}
    db = SessionLocal()
    try:
        org = (db.query(ApiKey).filter(ApiKey.scope == "data").first()).org_id
        endpoint = WebhookEndpoint(id=_uid(), org_id=org, url="http://127.0.0.1:1/hook",
                                   secret="whsec_test", events=["*"])
        db.add(endpoint)
        db.commit()

        class _FakeBG:
            def add_task(self, fn, *args):
                pass                      # do not deliver inline; drive attempts manually

        emit(db, _FakeBG(), event="LinkedAccount.linked", org_id=org, payload_data={"n": 1})
        delivery = (db.query(WebhookDelivery).filter(WebhookDelivery.endpoint_id == endpoint.id)
                    .order_by(WebhookDelivery.created_at.desc()).first())
        assert delivery.attempts == 0 and delivery.next_attempt_at is not None
        delivery_id = delivery.id
    finally:
        db.close()

    # attempt 1 fails (connection refused) -> backoff scheduled
    assert attempt_delivery(delivery_id) is False
    db = SessionLocal()
    try:
        for expected_attempts in range(2, MAX_ATTEMPTS + 1):
            d = db.get(WebhookDelivery, delivery_id)
            assert d.attempts == expected_attempts - 1
            d.next_attempt_at = _now() - timedelta(seconds=1)   # force backoff to elapse
            db.commit()
            assert any(x.id == delivery_id for x in due_retries(db))
            assert attempt_delivery(delivery_id) is False
            db.expire_all()
        d = db.get(WebhookDelivery, delivery_id)
        assert d.attempts == MAX_ATTEMPTS
        assert d.next_attempt_at is None, "dead after MAX_ATTEMPTS"
        assert all(x.id != delivery_id for x in due_retries(db))
    finally:
        db.close()


def test_response_side_dlp_redacts_model_output(client):
    """The echo model repeats the prompt; response-side DLP must redact the email
    before the completion reaches the caller."""
    from connexa.db import SessionLocal
    from connexa.models import ApiKey, SecurityRule
    from connexa.utils import uid as _uid

    db = SessionLocal()
    try:
        org_id = db.query(ApiKey).filter(ApiKey.scope == "gateway").first().org_id
        db.add(SecurityRule(id=_uid(), agent_org_id=org_id, entity_type="EMAIL",
                            name="emails-out", pattern=r"[\w.+-]+@[\w-]+\.[\w.]+", action="REDACT",
                            scope="org", is_active=True))
        db.commit()
    finally:
        db.close()

    sent = client.post("/v1/chat/completions",
                       headers={"Authorization": f"Bearer {_gateway_key(client)}"},
                       json={"model": "connexa-echo",
                             "messages": [{"role": "user", "content": "ping ciso@acme.example"}]})
    assert sent.status_code == 200
    body = sent.json()
    content = body["choices"][0]["message"]["content"]
    assert "ciso@acme.example" not in content, "model output must be redacted before leaving the gateway"
    assert "[REDACTED_EMAIL]" in content


def test_link_flow_customization(client):
    """Link tokens carry theming; the end-user connect page renders it."""
    headers = {"Authorization": f"Bearer {_workforce_key(client)}"}
    created = client.post("/api/link-tokens", headers=headers, json={
        "categories": ["hris"], "company_name": "Acme Corp", "accent_color": "#00a854",
        "logo_url": "https://acme.example/logo.png", "welcome_text": "Acme needs ERP access."})
    assert created.status_code == 200
    body = created.json()
    assert body["customize"]["company_name"] == "Acme Corp"

    page_html = client.get(f"/link/{body['link_token']}")
    assert page_html.status_code == 200
    text = page_html.text
    assert "#00a854" in text and "Acme Corp" in text and "Acme needs ERP access." in text
    assert "https://acme.example/logo.png" in text

    bad = client.post("/api/link-tokens", headers=headers, json={"accent_color": "green"})
    assert bad.status_code == 400, "accent_color must be #RRGGBB"


# ---------------------------------------------------------------- v0.5: self-serve SaaS + real data lane

def test_org_signup_login_plan_limits_billing(client):
    """Anyone can become a tenant: signup -> live keys, plans enforced at connect
    time (free = 3 linked accounts), login returns the account view, billing shows
    real usage against real limits."""
    signup = client.post("/api/v1/org/signup", json={
        "email": "owner@nitestream.example", "password": "correct-horse-battery",
        "company_name": "NiteStream"})
    assert signup.status_code == 201
    body = signup.json()
    assert body["plan"] == "free" and body["slug"] == "nitestream"
    assert body["data_api_key"].startswith("cxk_") and body["gateway_api_key"].startswith("cxg_")

    # the new tenant's keys work immediately on the real data plane
    headers = {"Authorization": f"Bearer {body['data_api_key']}"}
    token = client.post("/api/link-tokens", json={"categories": ["ticketing"]},
                        headers=headers)
    assert token.status_code == 200, "a self-serve org must be able to mint link tokens"
    token = token.json()["link_token"]

    # connect 3 accounts (free limit) -> 4th refused with 402 plan_limit_reached
    for i in range(3):
        connected = client.post(f"/link/{token}/connect", json={
            "integration": "helpdesk", "end_user_name": f"Tenant {i}"})
        assert connected.status_code == 200, connected.text
        assert client.post(f"/link/{token}/connect", json={
            "integration": "helpdesk", "end_user_name": f"Reuse {i}"}).status_code == 410, \
            "a link token is single-use"
        token = client.post("/api/link-tokens", json={"categories": ["ticketing"]},
                            headers=headers).json()["link_token"]

    blocked = client.post(f"/link/{token}/connect", json={
        "integration": "helpdesk", "end_user_name": "Overflow"})
    assert blocked.status_code == 402
    assert blocked.json()["detail"]["error"]["code"] == "plan_limit_reached"
    assert blocked.json()["detail"]["error"]["limit"] == 3

    # login: wrong password refused; right password returns the account view, keys masked
    assert client.post("/api/v1/org/login", json={
        "email": "owner@nitestream.example", "password": "wrong-password"}).status_code == 401
    login = client.post("/api/v1/org/login", json={
        "email": "owner@nitestream.example", "password": "correct-horse-battery"})
    assert login.status_code == 200
    login_body = login.json()
    assert login_body["org"] == "NiteStream" and login_body["plan"] == "free"
    masked = {k["masked"] for k in login_body["api_keys"]}
    assert any(m.startswith("cxk_") for m in masked)
    assert all(body["data_api_key"] not in m for m in masked), "full keys never come back on login"

    # account view + billing: real usage against real limits
    me = client.get("/api/v1/org/me", headers=headers)
    assert me.status_code == 200
    me_body = me.json()
    assert me_body["linked_accounts"] == {"used": 3, "limit": 3}
    assert me_body["plan"] == "free"

    billing = client.get("/api/v1/org/billing", headers=headers)
    assert billing.status_code == 200
    billing_body = billing.json()
    assert billing_body["plan"] == "free"
    assert billing_body["limits"] == {"linked_accounts": 3}
    assert billing_body["usage"]["linked_accounts"] == 3
    assert billing_body["payments_configured"] is False, "no Stripe link configured in tests"
    assert "upgrade" in billing_body["note"].lower()

    dup = client.post("/api/v1/org/signup", json={
        "email": "owner@nitestream.example", "password": "another-password-1",
        "company_name": "NiteStream Two"})
    assert dup.status_code == 409, "duplicate email refused"


def test_github_issues_real_lane(client):
    """The first REAL data lane: github-issues pulls live GitHub issues into the
    Ticket Common Model. Mapping proven against a real GitHub payload shape (HTTP
    stubbed), full sync path proven via a stubbed connector, and the link page
    advertises it as LIVE."""
    import httpx as _httpx

    from connexa.connectors import real_github_issues as rgi
    from connexa.connectors.base import Connector, RealPullError
    from connexa.connectors.real_github_issues import GitHubIssuesConnector
    from connexa.connectors.registry import registry

    headers = {"Authorization": f"Bearer {_workforce_key(client)}"}

    # ---- payload mapping: a real GitHub list-issues response -> Common Model rows
    github_payload = [
        {"number": 101, "title": "Login loop on Safari", "state": "open",
         "assignee": {"login": "dana"}, "labels": [{"name": "bug"}, {"name": "priority:high"}],
         "body": "Steps to reproduce...", "html_url": "https://github.com/o/r/issues/101",
         "created_at": "2026-11-01T10:00:00Z"},
        {"number": 102, "title": "Dark mode", "state": "closed", "assignee": None,
         "labels": [], "body": None, "html_url": "https://github.com/o/r/issues/102",
         "created_at": "2026-11-02T10:00:00Z"},
        {"number": 103, "title": "A pull request", "state": "open", "pull_request": {"url": "x"},
         "labels": [], "body": "not a ticket", "html_url": "u", "created_at": "2026-11-03T10:00:00Z"},
    ]

    class _FakeResponse:
        status_code = 200
        def __init__(self, payload):
            self._payload = payload
        def json(self):
            return self._payload

    calls = []

    def _fake_get(url, headers=None, params=None, timeout=None):
        calls.append((url, headers, params))
        return _FakeResponse(github_payload)

    original_get = rgi.httpx.get
    rgi.httpx.get = _fake_get
    try:
        connector = GitHubIssuesConnector(env_token="platform-token")
        rows = connector.pull_remote("ticketing", {"token": "ghp_user", "repo": "o/r"})
        assert len(rows) == 2, "pull requests are not tickets"
        first = next(r for r in rows if r["remote_id"] == "GH-101")
        assert first["title"] == "Login loop on Safari"
        assert first["status"] == "open" and first["state"] == "open"
        assert first["assignee"] == "dana"
        assert first["labels"] == ["bug", "priority:high"] and first["tags"] == ["bug", "priority:high"]
        assert first["description"] == "Steps to reproduce..."
        assert first["url"].endswith("/101")
        closed = next(r for r in rows if r["remote_id"] == "GH-102")
        assert closed["status"] == "closed"
        assert calls[0][0].endswith("/repos/o/r/issues") and calls[0][2]["state"] == "all"
        assert calls[0][1]["Authorization"] == "Bearer ghp_user", "the account's own credential wins"

        # platform token used when the account has none
        connector.pull_remote("ticketing", {"repo": "o/r"})
        assert calls[-1][1]["Authorization"] == "Bearer platform-token"
    finally:
        rgi.httpx.get = original_get

    # ---- error contract: never fabricate, always RealPullError
    class _StatusResponse:
        def __init__(self, status_code):
            self.status_code = status_code
        def json(self):
            return []

    for bad_credentials, api_status in [({"repo": "o/r"}, None),                      # no token anywhere
                                        ({"token": "x", "repo": "o/r"}, 401),         # provider rejects
                                        ({"token": "x", "repo": "just-a-name"}, None)]:  # malformed repo
        if api_status:
            rgi.httpx.get = lambda *a, _s=api_status, **k: _StatusResponse(_s)
        try:
            with pytest.raises(RealPullError):
                GitHubIssuesConnector().pull_remote("ticketing", bad_credentials)
        finally:
            rgi.httpx.get = original_get

    # ---- connect validation: a real lane demands credential + repo
    token = client.post("/api/link-tokens", json={"categories": ["ticketing"]},
                        headers=headers).json()["link_token"]
    no_cred = client.post(f"/link/{token}/connect", json={
        "integration": "github-issues", "end_user_name": "GH"})
    assert no_cred.status_code == 400 and "credential" in no_cred.json()["detail"]["detail"]
    cred_only = client.post(f"/link/{token}/connect", json={
        "integration": "github-issues", "end_user_name": "GH", "credential": "ghp_x"})
    assert cred_only.status_code == 400 and "repo" in cred_only.json()["detail"]["detail"]

    # ---- the link page advertises the real lane as LIVE
    page = client.get(f"/link/{client.post('/api/link-tokens', json={'categories': ['ticketing']},
                                         headers=headers).json()['link_token']}")
    assert page.status_code == 200
    assert "GitHub Issues" in page.text and "LIVE" in page.text

    # ---- full sync path through the real-lane branch of run_sync (stubbed pull)
    class _StubGitHubIssues(Connector):
        slug = "github-issues"
        name = "GitHub Issues"
        auth_type = "token"
        real_lane_ready = True
        data_categories = ["ticketing"]

        def __init__(self, rows, fail=False):
            super().__init__()
            self._rows = rows
            self._fail = fail

        def build_tools(self):
            return []

        def generate_sample(self, category):
            return []

        def pull_remote(self, category, credentials):
            if self._fail:
                raise RealPullError("GitHub rejected the token (401)")
            return self._rows

    rows_for_sync = [
        {"remote_id": "GH-101", "title": "Login loop on Safari", "status": "open",
         "description": "Steps...", "type": "ticket"},
        {"remote_id": "GH-102", "title": "Dark mode", "status": "closed",
         "description": "", "type": "ticket"},
    ]
    stub = _StubGitHubIssues(rows_for_sync)
    original_connector = registry._by_slug["github-issues"]
    registry._by_slug["github-issues"] = stub
    try:
        sync_token = client.post("/api/link-tokens", json={"categories": ["ticketing"]},
                                 headers=headers).json()["link_token"]
        connected = client.post(f"/link/{sync_token}/connect", json={
            "integration": "github-issues", "end_user_name": "GH",
            "credential": "ghp_real", "config": {"repo": "o/r"}})
        assert connected.status_code == 200, connected.text
        public_token = connected.json()["public_token"]

        summary = client.post(f"/api/ticketing/v1/accounts/{public_token}/sync", headers=headers)
        assert summary.status_code == 200
        body = summary.json()
        assert body["integration"] == "github-issues"
        # the connect flow already ran the initial sync (real pull) — an explicit
        # re-sync must see the same rows as unchanged (idempotent, never duplicated)
        assert body["added"] + body["unchanged"] == 2
        assert body["counts"]["Ticket"] == 2
        assert body["errors"] == 0

        tickets = client.get("/api/ticketing/v1/tickets",
                             headers={**headers, "X-Account-Token": public_token}).json()
        assert tickets["count"] == 2, "real pull: exactly the stub rows, no fabricated extras"
        titles = {t["title"] for t in tickets["results"]}
        assert {"Login loop on Safari", "Dark mode"} <= titles

        # provider breaks -> a sync issue is recorded and nothing is fabricated
        stub._fail = True
        summary = client.post(f"/api/ticketing/v1/accounts/{public_token}/sync", headers=headers).json()
        assert summary["errors"] == 1 and summary["added"] == 0
        issues = client.get("/api/ticketing/v1/issues", headers=headers).json()
        assert any("real pull failed" in i["message"] for i in issues["issues"])

        # a failing real pull DURING connect must still leave a usable (empty) account
        fail_token = client.post("/api/link-tokens", json={"categories": ["ticketing"]},
                                 headers=headers).json()["link_token"]
        fail_conn = client.post(f"/link/{fail_token}/connect", json={
            "integration": "github-issues", "end_user_name": "GH2",
            "credential": "ghp_bad", "config": {"repo": "o/r"}})
        assert fail_conn.status_code == 200
        fail_pt = fail_conn.json()["public_token"]
        fail_summary = client.post(f"/api/ticketing/v1/accounts/{fail_pt}/sync",
                                   headers=headers).json()
        assert fail_summary["errors"] == 1 and fail_summary["counts"] == {}
        empty = client.get("/api/ticketing/v1/tickets",
                           headers={**headers, "X-Account-Token": fail_pt}).json()
        assert empty["count"] == 0, "the account survives a failed pull and holds nothing fabricated"
    finally:
        registry._by_slug["github-issues"] = original_connector



