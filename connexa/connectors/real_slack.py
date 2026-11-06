"""Real Slack connector — live lanes, in priority order:
1. per-account OAuth grant (social login) or user-pasted bot token (via the API),
2. platform-wide CONNEXA_SLACK_TOKEN (Bot User OAuth Token),
3. demo lane otherwise.
Tools: send_message, list_channels (+ standard meta tools).
"""
from __future__ import annotations

import httpx
from pydantic import BaseModel

from .base import Connector, ToolSpec, ToolContext, credential_token, OAuthExchangeError


class SendMessageInput(BaseModel):
    channel: str
    text: str


class ListChannelsInput(BaseModel):
    limit: int = 20


class SlackConnector(Connector):
    slug = "slack"
    name = "Slack"
    description = ("Send messages and read channels in Slack. Live via OAuth sign-in, a pasted bot token, "
                   "or a platform token; sandbox demo otherwise.")
    auth_type = "token"
    real_lane_ready = False

    oauth_authorize_url = "https://slack.com/oauth/v2/authorize"
    oauth_token_url = "https://slack.com/api/oauth.v2.access"

    def __init__(self, env_token: str = "", oauth_config: dict | None = None,
                 api_base: str = "https://slack.com",
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
                                 scopes="chat:write,channels:read,channels:join")
        self.real_lane_ready = bool(env_token) or self.oauth_ready
        super().__init__()

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        response = httpx.post(self.oauth_token_url,
                              data={"client_id": self.oauth_client_id,
                                    "client_secret": self.oauth_client_secret,
                                    "code": code, "redirect_uri": redirect_uri},
                              timeout=15.0)
        data = response.json()
        if not data.get("ok"):
            raise OAuthExchangeError(str(data.get("error") or "provider_exchange_failed"))
        team = data.get("team") or {}
        bot = data.get("bot") or {}
        return {"access_token": data.get("access_token"), "token_type": "bot",
                "team_id": team.get("id"), "bot_user_id": bot.get("bot_user_id"),
                "account_label": team.get("name") or "workspace"}

    def describe_token(self, token: str) -> str | None:
        try:
            data = httpx.post(f"{self.api_base}/api/auth.test",
                              headers={"Authorization": f"Bearer {token}"}, timeout=10.0).json()
            if data.get("ok"):
                return data.get("team") or data.get("user")
        except Exception:  # noqa: BLE001
            return None
        return None

    def build_tools(self) -> list[ToolSpec]:
        def send_message(ctx: ToolContext, inp: SendMessageInput) -> dict:
            token = credential_token(ctx)
            if not token:
                return {"mode": "demo", "delivered": True, "channel": inp.channel,
                        "text_preview": inp.text[:140],
                        "note": "DEMO delivery — logged as if sent to Slack. Connect Slack (sign-in or paste a bot token) for live sends."}
            # Slack apps post only into channels they're in; best-effort join for
            # public channels (needs channels:write scope — silently skipped if absent).
            httpx.post(f"{self.api_base}/api/conversations.join",
                       headers={"Authorization": f"Bearer {token}",
                                "Content-Type": "application/json; charset=utf-8"},
                       json={"channel": inp.channel}, timeout=10.0)
            response = httpx.post(
                f"{self.api_base}/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json; charset=utf-8"},
                json={"channel": inp.channel, "text": inp.text}, timeout=10.0)
            data = response.json()
            if data.get("ok"):
                return {"mode": "live", "delivered": True, "channel": data.get("channel"), "ts": data.get("ts")}
            return {"mode": "live", "delivered": False, "slack_error": data.get("error", "unknown_error")}

        def list_channels(ctx: ToolContext, inp: ListChannelsInput) -> dict:
            token = credential_token(ctx)
            if not token:
                return {"mode": "demo", "channels": [{"id": "C0DEMO1", "name": "general"},
                                                     {"id": "C0DEMO2", "name": "integrations"}],
                        "note": "DEMO channel list. Connect Slack (sign-in or paste a bot token) for live data."}
            response = httpx.get(f"{self.api_base}/api/conversations.list",
                                 headers={"Authorization": f"Bearer {token}"},
                                 params={"limit": inp.limit}, timeout=10.0)
            data = response.json()
            channels = [{"id": c.get("id"), "name": c.get("name")}
                        for c in (data.get("channels") or [])]
            return {"mode": "live", "channels": channels, "ok": data.get("ok")}

        return [
            ToolSpec(name="send_message", description="Send a message to a Slack channel (live when connected).",
                     handler=send_message, input_model=SendMessageInput, credit_type="medium",
                     annotations={"readOnlyHint": False}),
            ToolSpec(name="list_channels", description="List public Slack channels the bot can see.",
                     handler=list_channels, input_model=ListChannelsInput, credit_type="low"),
        ]
