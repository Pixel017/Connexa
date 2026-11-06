"""MCP server over JSON-RPC 2.0 (SSE framing when the client asks; plain JSON otherwise)
plus the in-line Security Gateway applied to every tool call.
"""
from __future__ import annotations

import json
import re
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import RegisteredUser, SecurityRule, ToolPack
from ..utils import request_id
from ..metering import usage, audit
from ..connectors.base import ToolContext
from ..connectors.registry import registry
from .handler import agent_key

router = APIRouter(prefix="/api/v1")

SERVER_INFO = {"name": "ToolPack MCP Server", "version": "0.1.0"}
PROTOCOL_VERSION = "2025-03-26"

LUHNISH_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,19}\d\b")


def _luhn_ok(candidate: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", candidate)]
    if not 12 <= len(digits) <= 19:
        return False
    checksum = 0
    for position, digit in enumerate(reversed(digits)):
        if position % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def apply_gateway(agent_org_id: str, text: str, db: Session) -> tuple[str, list[dict], bool]:
    """Run active security rules over `text`; returns (final_text, violations, blocked)."""
    rules = (
        db.query(SecurityRule)
        .filter(SecurityRule.agent_org_id == agent_org_id, SecurityRule.is_active == True)  # noqa: E712
        .all()
    )
    violations: list[dict] = []
    blocked = False
    for rule in rules:
        matches = re.findall(rule.pattern, text)
        if rule.entity_type == "CREDIT_CARD":
            matches = [match for match in matches if _luhn_ok(match)]
        if not matches:
            continue
        violations.append({"rule": rule.entity_type, "action": rule.action, "hits": len(matches)})
        if rule.action == "BLOCK":
            blocked = True
        elif rule.action == "REDACT":
            text = re.sub(rule.pattern, f"[REDACTED_{rule.entity_type}]", text)
    return text, violations, blocked


def reauth_payload(tool_name: str, slug: str) -> dict:
    return {
        "error_reason": "reauth_required",
        "error_message": (f"Your credentials are missing or invalid. "
                          f"Call the authenticate_{slug} tool to reauthenticate."),
        "data": {"tool_name": tool_name, "reauth_reason": "not_connected",
                 "authenticate_tool": f"authenticate_{slug}"},
        "error_code": 401,
    }


def _outer_schema(t) -> dict:
    from .handler import _outer_schema as outer
    return outer(t)


def _tool_index(pack: ToolPack) -> dict:
    """full tool name -> (slug, connector, ToolSpec); includes auth/validate meta-tools."""
    index: dict[str, tuple[str, object, object]] = {}
    for slug in pack.connectors:
        connector = registry.get(slug)
        if not connector:
            continue
        for spec in connector.tools:
            if spec.name == "validate_credential":
                index[f"{slug}__validate_credential"] = (slug, connector, spec)
            elif spec.name == "authenticate":
                index[f"authenticate_{slug}"] = (slug, connector, spec)
            else:
                index[f"{slug}__{spec.name}"] = (slug, connector, spec)
    return index


def _mcp_result(content_text: str, is_error: bool, extra_meta: dict | None = None) -> dict:
    meta = {"dev.connexa/request_id": request_id()}
    if extra_meta:
        meta.update(extra_meta)
    result = {"content": [{"type": "text", "text": content_text}], "isError": is_error, "_meta": meta}
    if extra_meta and "violations" in extra_meta:
        result["violations"] = extra_meta["violations"]
    return result


@router.post("/tool-packs/{pack_id}/registered-users/{user_id}/mcp/")
async def mcp_endpoint(pack_id: str, user_id: str, request: Request,
                       auth=Depends(agent_key), db: Session = Depends(get_db)):
    key, org = auth
    pack = db.query(ToolPack).filter(ToolPack.id == pack_id, ToolPack.agent_org_id == org.id).first()
    if not pack:
        raise HTTPException(404, {"detail": "Unknown tool pack for this agent org."})
    user = db.query(RegisteredUser).filter(RegisteredUser.id == user_id,
                                           RegisteredUser.agent_org_id == org.id).first()
    if not user:
        raise HTTPException(404, {"detail": "Unknown registered user for this agent org."})

    try:
        message = await request.json()
    except Exception as error:  # noqa: BLE001
        raise HTTPException(400, {"detail": f"Malformed JSON-RPC payload: {error}"}) from error

    wants_sse = "text/event-stream" in (request.headers.get("accept") or "")

    def reply(payload: dict, session_id: str | None = None):
        if wants_sse:
            body = f"data: {json.dumps(payload)}\n\n"
            headers = {"mcp-session-id": session_id} if session_id else None
            return Response(content=body, media_type="text/event-stream", headers=headers)
        header = {"mcp-session-id": session_id} if session_id else None
        return JSONResponse(content=payload, headers=header)

    method = message.get("method")
    msg_id = message.get("id")

    if method is None:
        return reply({"jsonrpc": "2.0", "id": None,
                      "error": {"code": -32600, "message": "Invalid Request: missing method."}})

    if method.startswith("notifications/"):
        return Response(status_code=202)

    if method == "initialize":
        params = message.get("params") or {}
        session_id = request_id().replace("-", "")
        return reply({"jsonrpc": "2.0", "id": msg_id,
                      "result": {"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": SERVER_INFO}}, session_id=session_id)

    if method == "tools/list":
        entries = []
        for full_name, (_, _connector, spec) in _tool_index(pack).items():
            entry = {"name": full_name, "description": spec.description,
                     "inputSchema": _outer_schema(spec), "annotations": spec.annotations,
                     "credit_type": spec.credit_type}
            entries.append(entry)
        return reply({"jsonrpc": "2.0", "id": msg_id,
                      "result": {"tools": entries}})

    if method == "tools/call":
        params = message.get("params") or {}
        tool_name = params.get("name") or ""
        arguments = params.get("arguments") or {}
        payload = run_tool(db, org, key, user, pack, tool_name, arguments,
                           base_url=str(request.base_url).rstrip("/"))
        return reply({"jsonrpc": "2.0", "id": msg_id, "result": payload})

    return reply({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32601, "message": f"Method '{method}' not found."}})


def run_tool(db: Session, org, key, user, pack: ToolPack, tool_name: str, arguments: dict,
             base_url: str = "") -> dict:
    index = _tool_index(pack)
    found = index.get(tool_name)
    if not found:
        return _mcp_result(json.dumps({"error_reason": "unknown_tool",
                                       "error_message": f"Tool '{tool_name}' is not in this tool pack.",
                                       "error_code": 404}), True)
    slug, connector, spec = found

    # ---- security gateway pass 1 (input) ---------------------------------------
    input_text = json.dumps(arguments, default=str)
    input_text, in_violations, in_blocked = apply_gateway(org.id, input_text, db)
    if in_blocked:
        audit(db, event_type="AGENT_TOOL_CALL_BLOCKED", description=tool_name, agent_org_id=org.id)
        return _mcp_result(json.dumps({"error_reason": "guardrail_blocked",
                                       "error_message": "Blocked by an active security rule before dispatch.",
                                       "violations": in_violations}), True,
                           extra_meta={"violations": in_violations})

    # ---- build tool context ------------------------------------------------------
    env_token = getattr(connector, "env_token", "") if connector.real_lane_ready else ""
    ctx = ToolContext(
        connector_slug=slug,
        credentials=dict(user.credentials.get(slug, {})),
        env_token=env_token,
        registered_user_id=user.id,
        agent_org_id=org.id,
        base_url=base_url,
    )

    def persist(credential: dict) -> None:
        merged = dict(user.credentials.get(slug, {}) or {})
        merged.update(credential)
        user.credentials = {**user.credentials, slug: merged}
        db.commit()

    ctx.persist_credentials = persist

    # ---- proactive token refresh (oauth grants with expiry) -----------------------
    refreshed = connector.maybe_refresh(ctx.credentials)
    if refreshed:
        ctx.credentials = refreshed
        user.credentials = {**user.credentials, slug: refreshed}
        db.commit()

    # ---- credential gate ---------------------------------------------------------
    is_meta_tool = spec.name in ("authenticate", "validate_credential")
    needs = spec.needs_credentials and connector.auth_type != "none" and not is_meta_tool
    has_access = bool(ctx.credentials) or bool(ctx.env_token)
    if needs and not has_access:
        return _mcp_result(json.dumps(reauth_payload(tool_name, slug)), True)

    # ---- validate arguments --------------------------------------------------------
    inp = arguments.get("input", {})
    if spec.input_model is not None:
        try:
            validated = spec.input_model.model_validate(inp)
        except ValidationError as error:
            summary = [{"loc": e.get("loc"), "msg": e.get("msg")} for e in error.errors()][:5]
            return _mcp_result(json.dumps({"error_reason": "invalid_arguments",
                                           "error_message": "Invalid tool input.",
                                           "model": spec.input_model.__name__,
                                           "details": summary,
                                           "hint": "Validate against tools/list inputSchema before calling."}),
                                  True)

    # ---- execute --------------------------------------------------------------------
    try:
        result = spec.handler(ctx, inp if spec.input_model is None else validated)
    except Exception as error:  # noqa: BLE001
        result = {"error_reason": "execution_failed", "error_message": str(error)[:300]}

    # ---- security gateway pass 2 (output) ----------------------------------------
    output_text = json.dumps(result, default=str)
    output_text, out_violations, out_blocked = apply_gateway(org.id, output_text, db)
    if out_blocked:
        audit(db, event_type="AGENT_TOOL_CALL_BLOCKED", description=f"{tool_name} (output)",
              agent_org_id=org.id)
        return _mcp_result(json.dumps({"error_reason": "guardrail_blocked",
                                       "error_message": "Tool output blocked by an active security rule.",
                                       "violations": out_violations}), True,
                           extra_meta={"violations": out_violations})

    # ---- meter + audit --------------------------------------------------------------
    usage(db, plane="agent", kind="tool_call", units=1.0, credit_type=spec.credit_type,
          org_id=ctx.agent_org_id, agent_org_id=org.id, key_id=key.id,
          meta={"tool": tool_name, "connector": slug})
    audit(db, event_type="AGENT_TOOL_CALL",
          description=f"{tool_name} -> {'ok' if not out_blocked and result.get('error_reason') != 'reauth_required' else 'error'}",
          actor=f"key:{key.name}", agent_org_id=org.id)
    if spec.needs_credentials and ctx.credentials and credentials_changed(ctx.credentials,
                                                                          user.credentials.get(slug, {})):
        persist(ctx.credentials)

    extra = {"violations": (in_violations + out_violations)} if (in_violations or out_violations) else None
    is_error = bool(isinstance(result, dict) and (
        result.get("error_reason") or result.get("reauth_required") or result.get("delivered") is False))
    return _mcp_result(output_text, is_error, extra_meta=extra)


def credentials_changed(old_ctx: dict, persisted: dict) -> bool:
    return old_ctx != persisted


@router.post("/tool-packs/{pack_id}/registered-users/{user_id}/search/")
def tool_search(pack_id: str, user_id: str, data: dict,
                auth=Depends(agent_key), db: Session = Depends(get_db)):
    """Natural-language tool discovery across the pack; governors get request_tool_access."""
    key, org = auth
    pack = db.query(ToolPack).filter(ToolPack.id == pack_id, ToolPack.agent_org_id == org.id).first()
    if not pack or not db.query(RegisteredUser).filter(RegisteredUser.id == user_id,
                                                       RegisteredUser.agent_org_id == org.id).first():
        raise HTTPException(404, {"detail": "Unknown tool pack or registered user for this agent org."})
    intent = str(data.get("intent", "")).strip()
    if not intent:
        raise HTTPException(400, {"intent": ["This field is required."]})
    words = intent.lower().replace("_", " ").split()
    matches = []
    pack_slugs = set(pack.connectors)
    for full_name, (slug, _connector, spec) in _tool_index(pack).items():
        hay = (full_name + " " + slug + " " + spec.description).lower()
        score = sum(1 for word in words if word and word in hay)
        if score:
            matches.append({"name": full_name, "description": spec.description, "score": score})
    matches.sort(key=lambda entry: (-entry["score"], entry["name"]))
    payload: dict = {"intent": intent, "matches": matches[:10]}
    from ..models import ToolAccessRequest
    requestable = [connector.slug for connector in registry.list_connectors()
                   if connector.slug not in pack_slugs]
    if not matches:
        payload["requestable_tools"] = requestable
        payload["pseudo_tool"] = "request_tool_access"
        payload["note"] = ("No pack tool matches this intent. POST /api/v1/tool-access-requests/ "
                           "with tool_name='request_tool_access' to ask a reviewer to widen the pack.")
    else:
        payload["requestable_tools"] = requestable
    usage(db, plane="agent", kind="tool_search", units=1.0, credits=0, org_id=org.id,
          agent_org_id=org.id, key_id=key.id, meta={"intent": intent[:120]})
    return payload
