"""REAL ticketing data lane — GitHub Issues -> Ticket Common Model.

No simulation: `pull_remote` calls the live GitHub REST API with the account's
own PAT (per-account, from the link flow) or the platform CONNEXA_GITHUB_TOKEN.
Every repository issue becomes a normalized Ticket row (status, labels, assignee,
body) — exactly like a commercial connector maps a provider onto a Common Model.
"""
from __future__ import annotations

import httpx

from .base import Connector, RealPullError


class GitHubIssuesConnector(Connector):
    slug = "github-issues"
    name = "GitHub Issues"
    description = ("REAL ticketing lane: sync real GitHub repository issues into the Ticket "
                   "Common Model. Connect with a personal access token and a repo (owner/name).")
    auth_type = "token"
    real_lane_ready = True
    data_categories = ["ticketing"]

    def __init__(self, env_token: str = "", api_base: str = "https://api.github.com") -> None:
        self.env_token = env_token
        self.api_base = api_base.rstrip("/")
        super().__init__()

    def build_tools(self):
        return []

    def generate_sample(self, category: str) -> list[dict]:
        return []      # real only — this connector never fabricates data

    # --- the real pull -----------------------------------------------------------
    def _token_for(self, credentials: dict) -> str:
        return (credentials.get("token") or credentials.get("access_token")
                or credentials.get("oauth_access_token") or str(self.env_token or ""))

    def passthrough(self, ctx, method: str, path: str) -> dict:
        """REAL passthrough: forward method+path to the live GitHub REST API with
        the account's credential (per-account PAT or platform token)."""
        token = self._token_for(ctx.credentials or {})
        if not token:
            return {"status": 401, "body": {"error": "A GitHub token is required for passthrough "
                                                    "(paste one or set CONNEXA_GITHUB_TOKEN)."}}
        url = f"https://api.github.com{path if path.startswith('/') else '/' + path}"
        try:
            response = httpx.request(method, url, headers={"Authorization": f"Bearer {token}",
                                                           "Accept": "application/vnd.github+json"},
                                     json=(getattr(ctx, "request_payload", None)
                                           if method not in ("GET", "DELETE") else None),
                                     timeout=15.0)
        except httpx.HTTPError as error:
            return {"status": 502, "body": {"error": f"github unreachable: {error}"}}
        try:
            body = response.json()
        except ValueError:
            body = response.text
        return {"status": response.status_code, "body": body}

    def pull_remote(self, category: str, credentials: dict) -> list[dict]:
        if category != "ticketing":
            raise RealPullError(f"github-issues does not serve category '{category}'.")
        token = self._token_for(credentials)
        repo = credentials.get("repo") or ""
        if not token:
            raise RealPullError("A GitHub token is required (paste one in the link flow or set "
                                "CONNEXA_GITHUB_TOKEN).")
        if not repo or repo.count("/") != 1:
            raise RealPullError("A repository in the form 'owner/name' is required.")

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        issues: list[dict] = []
        page = 1
        while page <= 3:                       # up to 300 issues per pull (3 x 100)
            response = httpx.get(f"{self.api_base}/repos/{repo}/issues",
                                 headers=headers,
                                 params={"state": "all", "per_page": 100, "page": page},
                                 timeout=15.0)
            if response.status_code == 401:
                raise RealPullError("GitHub rejected the token (401) — paste a fresh PAT.")
            if response.status_code == 404:
                raise RealPullError(f"Repository '{repo}' not found (404) — check owner/name "
                                    f"and the token's repo scope.")
            if response.status_code != 200:
                raise RealPullError(f"GitHub API returned HTTP {response.status_code}.")
            batch = response.json()
            issues.extend(batch)
            if len(batch) < 100:
                break
            page += 1

        # provider payload -> Common Model row (provider-specific names included so
        # field mappings can rename them; verdicts computed on normalized fields)
        rows = []
        for issue in issues:
            if "pull_request" in issue:        # PRs are not tickets
                continue
            labels = [label.get("name") for label in issue.get("labels") or []]
            rows.append({
                "remote_id": f"GH-{issue.get('number')}",
                "title": issue.get("title") or "Untitled",
                "state": issue.get("state") or "open",        # provider name (mappable)
                "status": "closed" if issue.get("state") == "closed" else "open",
                "assignee": ((issue.get("assignee") or {}).get("login")),
                "labels": labels,                              # provider name (mappable)
                "tags": labels,
                "description": issue.get("body") or "",
                "url": issue.get("html_url"),
                "created": issue.get("created_at"),
                "type": "ticket",
            })
        return rows
