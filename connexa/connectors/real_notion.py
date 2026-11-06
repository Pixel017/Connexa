"""Real Notion connector — live lanes, in priority order:
1. per-account OAuth grant (social login) or user-pasted integration token,
2. platform-wide CONNEXA_NOTION_TOKEN,
3. demo lane otherwise.
"""
from __future__ import annotations

import base64

import httpx
from pydantic import BaseModel

from .base import Connector, ToolSpec, ToolContext, credential_token, OAuthExchangeError


class SearchInput(BaseModel):
    query: str
    limit: int = 5


class CreatePageInput(BaseModel):
    parent_page_id: str
    title: str
    content: str = ""


class NotionConnector(Connector):
    slug = "notion"
    name = "Notion"
    description = ("Search Notion and append pages. Live via OAuth sign-in, a pasted integration token, "
                   "or a platform token; sandbox demo otherwise.")
    auth_type = "token"
    real_lane_ready = False

    oauth_authorize_url = "https://api.notion.com/v1/oauth/authorize"
    oauth_token_url = "https://api.notion.com/v1/oauth/token"

    def __init__(self, env_token: str = "", oauth_config: dict | None = None,
                 api_base: str = "https://api.notion.com",
                 authorize_url: str = "", token_url: str = "") -> None:
        self.env_token = env_token
        self.api_base = api_base.rstrip("/")
        if authorize_url:
            self.oauth_authorize_url = authorize_url
        if token_url:
            self.oauth_token_url = token_url
        if oauth_config and oauth_config.get("client_id") and oauth_config.get("client_secret"):
            self.configure_oauth(client_id=oauth_config["client_id"],
                                 client_secret=oauth_config["client_secret"],
                                 extra_auth_params={"owner": "user"})
        self.real_lane_ready = bool(env_token) or self.oauth_ready
        super().__init__()

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        basic = base64.b64encode(f"{self.oauth_client_id}:{self.oauth_client_secret}".encode()).decode()
        response = httpx.post(self.oauth_token_url,
                              headers={"Authorization": f"Basic {basic}",
                                       "Content-Type": "application/json",
                                       "Accept": "application/json"},
                              json={"grant_type": "authorization_code", "code": code,
                                    "redirect_uri": redirect_uri},
                              timeout=15.0)
        data = response.json()
        if not data.get("access_token"):
            raise OAuthExchangeError(str(data.get("error_description") or data.get("error") or "provider_exchange_failed"))
        return {"access_token": data["access_token"], "token_type": data.get("token_type", "bearer"),
                "workspace_id": data.get("workspace_id"),
                "account_label": data.get("workspace_name") or "workspace"}

    def describe_token(self, token: str) -> str | None:
        try:
            data = httpx.get(f"{self.api_base}/v1/users/me",
                             headers={"Authorization": f"Bearer {token}",
                                      "Notion-Version": "2022-06-28"}, timeout=10.0).json()
            if data.get("object") == "user":
                return data.get("name") or (data.get("bot") or {}).get("workspace_name")
        except Exception:  # noqa: BLE001
            return None
        return None

    def build_tools(self) -> list[ToolSpec]:
        def search(ctx: ToolContext, inp: SearchInput) -> dict:
            token = credential_token(ctx)
            if not token:
                return {"mode": "demo", "results": [
                    {"id": "demo-page-1", "title": f"Demo page matching '{inp.query}'", "type": "page"},
                    {"id": "demo-db-1", "title": "Team database", "type": "database"}],
                    "note": "DEMO search. Connect Notion (sign-in or paste an integration token) for live search."}
            response = httpx.post(f"{self.api_base}/v1/search",
                                  headers={"Authorization": f"Bearer {token}",
                                           "Notion-Version": "2022-06-28",
                                           "Content-Type": "application/json"},
                                  json={"query": inp.query, "page_size": inp.limit}, timeout=10.0)
            data = response.json()
            results = []
            for item in data.get("results", []):
                text = ""
                for title in item.get("properties", {}).get("title", {}).get("title", []) or []:
                    text += title.get("plain_text", "")
                results.append({"id": item.get("id"), "type": item.get("object"), "title": text})
            return {"mode": "live", "results": results}

        def create_page(ctx: ToolContext, inp: CreatePageInput) -> dict:
            token = credential_token(ctx)
            if not token:
                return {"mode": "demo", "created": True, "title": inp.title,
                        "note": "DEMO write — logged as if written to Notion. Connect Notion for live writes."}
            response = httpx.post(f"{self.api_base}/v1/pages",
                                  headers={"Authorization": f"Bearer {token}",
                                           "Notion-Version": "2022-06-28",
                                           "Content-Type": "application/json"},
                                  json={"parent": {"page_id": inp.parent_page_id},
                                        "properties": {"title": [{"text": {"content": inp.title}}]},
                                        "children": [
                                            {"object": "block", "type": "paragraph",
                                             "paragraph": {"rich_text": [{"text": {"content": inp.content}}]}}
                                        ]},
                                  timeout=10.0)
            data = response.json()
            if "id" in data:
                return {"mode": "live", "created": True, "id": data["id"], "url": data.get("url")}
            return {"mode": "live", "created": False, "error": data.get("code", "unknown")}

        return [
            ToolSpec(name="search", description="Search pages/databases visible to the integration.",
                     handler=search, input_model=SearchInput, credit_type="low"),
            ToolSpec(name="create_page", description="Append a page with one paragraph under a parent page.",
                     handler=create_page, input_model=CreatePageInput, credit_type="medium",
                     annotations={"readOnlyHint": False}),
        ]
