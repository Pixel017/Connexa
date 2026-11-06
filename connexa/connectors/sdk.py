"""Connector SDK — declare a REST connector in minutes instead of writing a class.

Two ways to add a connector to Connexa:

1. **Declarative (this module)** — for providers that are plain REST + a bearer
   token: describe each tool as a `RestCall` and instantiate
   `SimpleRestConnector`. You get the demo lane, the `reauth_required`
   contract, the authenticate/validate meta-tools, OAuth support, metering and
   audit for free. See `docs/connector-sdk.md` for a copy-paste template.

2. **Full class** (`real_slack.py` / `real_github.py` style) — when a provider
   needs custom request shapes, multi-step flows or OAuth quirks, subclass
   `Connector` directly and write `build_tools()` by hand.

Both paths land in the same registry: catalog, tool packs, search, the
security gateway and metering pick the connector up automatically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from pydantic import BaseModel

from .base import Connector, ToolSpec, ToolContext, credential_token


@dataclass
class RestCall:
    """One agent-callable tool backed by a single REST call.

    `build_request(ctx, inp)` may be provided for custom request shaping; the
    default sends `inp.model_dump(exclude_none=True)` as the query (GET/DELETE)
    or JSON body (POST/PATCH/PUT).
    """
    name: str
    description: str
    path: str                                   # appended to api_base, e.g. "/v1/items"
    method: str = "GET"
    input_model: type[BaseModel] | None = None
    demo_results: list[dict] = field(default_factory=list)   # demo lane response body
    demo_note: str = "DEMO response — connect this connector for live calls."
    credit_type: str = "low"
    read_only: bool = True
    build_request: Callable[[ToolContext, Any], tuple[str, str, dict | None]] | None = None
    # ^ custom builder returns (method, url, json_body_or_None)


def _default_request(api_base: str, call: RestCall, inp: Any) -> tuple[str, str, dict | None]:
    url = f"{api_base}{call.path}"
    body = None
    if call.input_model is not None and inp is not None:
        payload = inp.model_dump(exclude_none=True)
        if call.method.upper() in ("GET", "DELETE"):
            url += f"?{__import__('urllib.parse', fromlist=['urlencode']).urlencode(payload)}"
        else:
            body = payload
    return call.method.upper(), url, body


class SimpleRestConnector(Connector):
    """A connector assembled from `RestCall` declarations.

    Subclass (or instantiate) it with `api_base` + `calls` and optionally
    `headers_factory` (token -> header dict; default `Authorization: Bearer`).
    Everything else — demo lane, reauth contract, OAuth lane — is inherited.
    """

    def __init__(self, *, api_base: str, calls: list[RestCall],
                 headers_factory: Callable[[str], dict] | None = None,
                 env_token: str = "", oauth_config: dict | None = None,
                 scopes: str = "", extra_auth_params: dict | None = None) -> None:
        self.env_token = env_token
        self.api_base = api_base.rstrip("/")
        self._calls = calls
        self._headers_factory = headers_factory or (lambda token: {
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        if oauth_config and oauth_config.get("client_id") and oauth_config.get("client_secret"):
            self.configure_oauth(client_id=oauth_config["client_id"],
                                 client_secret=oauth_config["client_secret"],
                                 scopes=scopes, extra_auth_params=extra_auth_params)
        self.real_lane_ready = bool(env_token) or self.oauth_ready
        super().__init__()

    def build_tools(self) -> list[ToolSpec]:
        connector = self

        def make_handler(call: RestCall) -> Callable[[ToolContext, Any], dict]:
            def handler(ctx: ToolContext, inp: Any) -> dict:
                token = credential_token(ctx)
                if not token:
                    return {"mode": "demo", "results": call.demo_results, "note": call.demo_note}
                method, url, body = (call.build_request(ctx, inp) if call.build_request
                                     else _default_request(connector.api_base, call, inp))
                response = httpx.request(method, url, headers=connector._headers_factory(token),
                                         json=body, timeout=15.0)
                try:
                    data = response.json()
                except ValueError:
                    data = {"raw": response.text[:2000]}
                if response.status_code >= 400:
                    detail = data.get("error") or data.get("message") or response.status_code
                    return {"mode": "live", "error": str(detail), "status": response.status_code}
                return {"mode": "live", "data": data}
            return handler

        return [ToolSpec(name=call.name, description=call.description,
                         handler=make_handler(call), input_model=call.input_model,
                         credit_type=call.credit_type,
                         annotations={"readOnlyHint": call.read_only, "destructiveHint": False,
                                      "idempotentHint": call.method.upper() == "GET",
                                      "openWorldHint": False})
                for call in self._calls]
