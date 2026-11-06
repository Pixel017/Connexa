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
