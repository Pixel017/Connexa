"""Real GitHub connector — live lanes, in priority order:
1. per-account OAuth grant (social login) or user-pasted PAT,
2. platform-wide CONNEXA_GITHUB_TOKEN,
3. demo lane otherwise.
The API base and OAuth base are overridable for offline mock-provider tests.
"""
from __future__ import annotations

import httpx
from pydantic import BaseModel

from .base import Connector, ToolSpec, ToolContext, credential_token, OAuthExchangeError


class CreateIssueInput(BaseModel):
    owner: str
    repo: str
    title: str
    body: str = ""


class ListIssuesInput(BaseModel):
    owner: str
    repo: str
    state: str = "open"
    limit: int = 10


class GetRepoInput(BaseModel):
    owner: str
    repo: str


class GitHubConnector(Connector):
    slug = "github"
    name = "GitHub"
    description = ("Read repos and manage issues on GitHub. Live via OAuth sign-in, a pasted PAT, "
                   "or a platform token; sandbox demo otherwise.")
    auth_type = "token"
    real_lane_ready = False

    oauth_authorize_url = "https://github.com/login/oauth/authorize"
    oauth_token_url = "https://github.com/login/oauth/access_token"

    def __init__(self, env_token: str = "", oauth_config: dict | None = None,
                 api_base: str = "https://api.github.com",
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
                                 scopes="repo read:user")
        self.real_lane_ready = bool(env_token) or self.oauth_ready
        super().__init__()

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        response = httpx.post(self.oauth_token_url,
                              headers={"Accept": "application/json"},
                              data={"client_id": self.oauth_client_id,
                                    "client_secret": self.oauth_client_secret,
                                    "code": code, "redirect_uri": redirect_uri},
                              timeout=15.0)
        data = response.json()
        if not data.get("access_token"):
            raise OAuthExchangeError(str(data.get("error_description") or data.get("error") or "provider_exchange_failed"))
        token = data["access_token"]
        return {"access_token": token, "token_type": data.get("token_type", "bearer"),
                "scope": data.get("scope", ""), "account_label": self.describe_token(token)}

    def describe_token(self, token: str) -> str | None:
        try:
            response = httpx.get(f"{self.api_base}/user",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/vnd.github+json"}, timeout=10.0)
            if response.status_code == 200:
                return response.json().get("login")
        except Exception:  # noqa: BLE001
            return None
        return None

    def build_tools(self) -> list[ToolSpec]:
        def create_issue(ctx: ToolContext, inp: CreateIssueInput) -> dict:
            token = credential_token(ctx)
            if not token:
                num = 400 + abs(hash((inp.owner, inp.repo, inp.title))) % 600
                return {"mode": "demo", "created": True,
                        "issue_url": f"https://github.com/{inp.owner}/{inp.repo}/issues/{num}",
                        "number": num, "title": inp.title,
                        "note": "DEMO write — logged as if created. Connect GitHub (sign-in or paste a PAT) for live issue creation."}
            response = httpx.post(
                f"{self.api_base}/repos/{inp.owner}/{inp.repo}/issues",
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"},
                json={"title": inp.title, "body": inp.body}, timeout=10.0)
            data = response.json()
            if response.status_code == 201:
                return {"mode": "live", "created": True, "number": data.get("number"), "url": data.get("html_url")}
            return {"mode": "live", "created": False, "error": data.get("message", f"http {response.status_code}")}

        def list_issues(ctx: ToolContext, inp: ListIssuesInput) -> dict:
            token = credential_token(ctx)
            if not token:
                return {"mode": "demo", "issues": [
                    {"number": 1, "title": "Demo issue one", "state": "open"},
                    {"number": 2, "title": "Demo issue two", "state": "closed"}],
                    "note": "DEMO list. Connect GitHub (sign-in or paste a PAT) for live data."}
            response = httpx.get(
                f"{self.api_base}/repos/{inp.owner}/{inp.repo}/issues",
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"},
                params={"state": inp.state, "per_page": inp.limit}, timeout=10.0)
            if response.status_code == 200:
                issues = [{"number": i.get("number"), "title": i.get("title"), "state": i.get("state")}
                          for i in response.json()]
                return {"mode": "live", "issues": issues}
            return {"mode": "live", "error": f"http {response.status_code}"}

        def get_repo(ctx: ToolContext, inp: GetRepoInput) -> dict:
            token = credential_token(ctx)
            if not token:
                return {"mode": "demo", "repo": {"full_name": f"{inp.owner}/{inp.repo}", "private": True,
                                                 "default_branch": "main"},
                        "note": "DEMO metadata."}
            response = httpx.get(f"{self.api_base}/repos/{inp.owner}/{inp.repo}",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/vnd.github+json"}, timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                return {"mode": "live", "repo": {"full_name": data.get("full_name"),
                                                  "default_branch": data.get("default_branch"),
                                                  "stars": data.get("stargazers_count"),
                                                  "private": data.get("private")}}
            return {"mode": "live", "error": f"http {response.status_code}"}

        return [
            ToolSpec(name="create_issue", description="Open a new GitHub issue on a repository.",
                     handler=create_issue, input_model=CreateIssueInput, credit_type="medium",
                     annotations={"readOnlyHint": False}),
            ToolSpec(name="list_issues", description="List issues on a repository.",
                     handler=list_issues, input_model=ListIssuesInput, credit_type="low"),
            ToolSpec(name="get_repository", description="Fetch repository metadata.",
                     handler=get_repo, input_model=GetRepoInput, credit_type="low"),
        ]
