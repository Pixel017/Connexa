"""Real Gmail connector — live lanes, in priority order:
1. per-account OAuth grant (social login) or user-pasted token,
2. platform-wide CONNEXA_GMAIL_TOKEN,
3. demo lane otherwise.

Google OAuth specifics handled here:
- authorize URL needs `access_type=offline&prompt=consent` so Google issues a
  refresh token (access tokens expire in 3600s; `maybe_refresh` renews them);
- scopes are space-separated full URIs;
- the standard form-encoded code exchange (base class) works as-is;
- the account label comes from the userinfo endpoint (email address).
"""
from __future__ import annotations

import base64

import httpx
from pydantic import BaseModel, Field

from .base import Connector, ToolSpec, ToolContext, credential_token

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

SCOPES = " ".join([
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
])


class SendEmailInput(BaseModel):
    to: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)


class ListMessagesInput(BaseModel):
    query: str = ""          # Gmail search syntax, e.g. "from:x is:unread"
    limit: int = Field(default=5, ge=1, le=20)


class GmailConnector(Connector):
    slug = "gmail"
    name = "Gmail"
    description = ("Read and send email. Live via Google OAuth sign-in or a pasted token; "
                   "sandbox demo otherwise.")
    auth_type = "token"
    real_lane_ready = False

    oauth_authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    oauth_token_url = "https://oauth2.googleapis.com/token"
    oauth_scopes = SCOPES

    def __init__(self, env_token: str = "", oauth_config: dict | None = None,
                 api_base: str = "https://gmail.googleapis.com") -> None:
        self.env_token = env_token
        self.api_base = api_base.rstrip("/")
        if oauth_config and oauth_config.get("client_id") and oauth_config.get("client_secret"):
            self.configure_oauth(client_id=oauth_config["client_id"],
                                 client_secret=oauth_config["client_secret"],
                                 scopes=SCOPES,
                                 extra_auth_params={"access_type": "offline", "prompt": "consent"})
        self.real_lane_ready = bool(env_token) or self.oauth_ready
        super().__init__()

    # --- OAuth helpers -------------------------------------------------------
    def _mime_message(self, to: str, subject: str, body: str) -> str:
        raw = f"To: {to}\r\nSubject: {subject}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}"
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    def describe_token(self, token: str) -> str | None:
        try:
            data = httpx.get("https://www.googleapis.com/oauth2/v3/userinfo",
                             headers={"Authorization": f"Bearer {token}"}, timeout=10.0).json()
            return data.get("email")
        except Exception:  # noqa: BLE001
            return None

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # --- tools ----------------------------------------------------------------
    def build_tools(self) -> list[ToolSpec]:
        def send_email(ctx: ToolContext, inp: SendEmailInput) -> dict:
            token = credential_token(ctx)
            if not token:
                return {"mode": "demo", "sent": True, "to": inp.to, "subject": inp.subject,
                        "note": "DEMO send — logged as if emailed. Connect Gmail (Google sign-in or "
                                "paste a token) for live sends."}
            response = httpx.post(f"{self.api_base}/gmail/v1/users/me/messages/send",
                                  headers=self._headers(token),
                                  json={"raw": self._mime_message(inp.to, inp.subject, inp.body)},
                                  timeout=15.0)
            data = response.json()
            if "id" in data:
                return {"mode": "live", "sent": True, "id": data["id"], "thread_id": data.get("threadId")}
            return {"mode": "live", "sent": False,
                    "error": data.get("error", {}).get("message", "gmail_error")}

        def list_messages(ctx: ToolContext, inp: ListMessagesInput) -> dict:
            token = credential_token(ctx)
            if not token:
                return {"mode": "demo", "results": [
                    {"id": "demo-msg-1", "subject": f"Demo message matching '{inp.query or 'inbox'}'"},
                    {"id": "demo-msg-2", "subject": "Re: demo"}],
                    "note": "DEMO search. Connect Gmail for live mailbox search."}
            params = {"maxResults": min(inp.limit, 20)}
            if inp.query:
                params["q"] = inp.query
            listed = httpx.get(f"{self.api_base}/gmail/v1/users/me/messages",
                               headers=self._headers(token), params=params, timeout=15.0).json()
            results = []
            for stub in listed.get("messages", [])[:inp.limit]:
                msg = httpx.get(f"{self.api_base}/gmail/v1/users/me/messages/{stub['id']}",
                                headers=self._headers(token),
                                params={"format": "metadata",
                                        "metadataHeaders": ["From", "Subject"]},
                                timeout=15.0).json()
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                results.append({"id": msg.get("id"), "from": headers.get("From", ""),
                                "subject": headers.get("Subject", ""), "snippet": msg.get("snippet", "")})
            if "error" in listed:
                return {"mode": "live", "results": [],
                        "error": listed["error"].get("message", "gmail_error")}
            return {"mode": "live", "result_size": listed.get("resultSizeEstimate"), "results": results}

        return [
            ToolSpec(name="list_messages", description="Search the mailbox (Gmail search syntax).",
                     handler=list_messages, input_model=ListMessagesInput, credit_type="low"),
            ToolSpec(name="send_email", description="Send an email as the connected account.",
                     handler=send_email, input_model=SendEmailInput, credit_type="medium",
                     annotations={"readOnlyHint": False}),
        ]
