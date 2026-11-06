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
