"""Gateway plane: model catalog, vendors, routing policies, OpenAI-compatible
chat completions with failover, budgets, and usage metering.
"""
from __future__ import annotations

import json
import uuid
from itertools import count

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Budget, GatewayModel, RoutingPolicy
from ..metering import audit, check_gateway_budget, usage
from ..utils import uid, iso
from . import providers

router = APIRouter(prefix="/v1")


def gateway_auth(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    from ..security import require_scope
    return require_scope("gateway")(authorization=authorization, db=db)


# ---------------------------------------------------------------- catalog

@router.get("/models")
def list_models(auth=Depends(gateway_auth), db: Session = Depends(get_db)):
    _key, _org = auth
    rows = db.query(GatewayModel).filter(GatewayModel.status != "retired").all()
    return {"object": "list", "data": [
        {"id": r.slug, "vendor": r.vendor, "family": r.family, "status": r.status,
         "context_window": r.context_window,
         "pricing_credits_per_1k": {"input": r.input_cpt, "output": r.output_cpt},
         "capabilities": r.capabilities} for r in rows]}


@router.get("/vendors")
def list_vendors(auth=Depends(gateway_auth), db: Session = Depends(get_db)):
    _key, _org = auth
    rows = db.query(GatewayModel).all()
    vendors: dict[str, dict] = {}
    for row in rows:
        entry = vendors.setdefault(row.vendor, {"vendor": row.vendor, "models": 0, "families": set()})
        entry["models"] += 1
        entry["families"].add(row.family)
    for entry in vendors.values():
        entry["families"] = sorted(entry["families"])
    return {"count": len(vendors), "vendors": sorted(vendors.values(), key=lambda v: v["vendor"])}

# ---------------------------------------------------------------- routing policies


@router.get("/routing-policies")
def list_policies(auth=Depends(gateway_auth), db: Session = Depends(get_db)):
    _key, org = auth
    rows = db.query(RoutingPolicy).filter(RoutingPolicy.org_id == org.id).all()
    return {"count": len(rows), "policies": [
        {"id": r.id, "name": r.name, "strategy": r.strategy,
         "model_order": r.model_order, "is_default": r.is_default} for r in rows]}


@router.post("/routing-policies", status_code=201)
def create_policy(data: dict, auth=Depends(gateway_auth), db: Session = Depends(get_db)):
    key, org = auth
    name = str(data.get("name") or "policy").strip()
    strategy = data.get("strategy", "priority")
    if strategy not in ("priority", "balanced"):
        raise HTTPException(400, {"detail": "strategy must be 'priority' or 'balanced'."})
    order = data.get("model_order") or []
    known = {row.slug for row in db.query(GatewayModel).all()}
    unknown = [m for m in order if m not in known]
    if unknown:
        raise HTTPException(400, {"detail": f"Unknown models in model_order: {unknown}"})
    make_default = bool(data.get("is_default"))
    if make_default:
        db.query(RoutingPolicy).filter(RoutingPolicy.org_id == org.id).update({"is_default": False})
    policy = RoutingPolicy(id=uid(), org_id=org.id, name=name, strategy=strategy,
                           model_order=order, is_default=make_default)
    db.add(policy)
    db.commit()
    audit(db, event_type="ROUTING_POLICY_CREATED", description=name, org_id=org.id, actor=f"key:{key.name}")
    return {"id": policy.id, "name": policy.name, "strategy": policy.strategy,
            "model_order": policy.model_order, "is_default": policy.is_default}

# ---------------------------------------------------------------- chat completions


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: int = 256
    stream: bool = False
    temperature: float | None = None


def _resolve_order(db: Session, org, model_name: str | None) -> list[GatewayModel]:
    """Routing resolution: explicit model -> default policy chain -> catalog order."""
    by_slug = {row.slug: row for row in db.query(GatewayModel).filter(GatewayModel.status == "available").all()}
    if model_name:
        chosen = by_slug.get(model_name)
        if not chosen:
            raise HTTPException(404, {"detail": f"Model '{model_name}' not found in catalog."})
        return [chosen]
    policy = (db.query(RoutingPolicy)
              .filter(RoutingPolicy.org_id == org.id, RoutingPolicy.is_default == True)  # noqa: E712
              .first())
    order: list[GatewayModel] = []
    if policy:
        for slug in policy.model_order:
            row = by_slug.get(slug)
            if row:
                order.append(row)
    for slug in ["connexa-echo"]:
        row = by_slug.get(slug)
        if row and row not in order:
            order.append(row)
    if not order:
        raise HTTPException(500, {"detail": "No models available."})
    return order


def _remaining_budget(db: Session, org) -> int:
    return check_gateway_budget(db, org.id)


@router.post("/chat/completions")
def chat_completions(request: ChatCompletionRequest, auth=Depends(gateway_auth),
                     db: Session = Depends(get_db),
                     x_connexa_team: str | None = Header(default=None, alias="X-Connexa-Team")):
    key, org = auth

    # ---- workforce governance: team allowlist + team budget (X-Connexa-Team) ----
    from ..workforce import enforce_team_model
    team = enforce_team_model(db, org, x_connexa_team, request.model)

    remaining = _remaining_budget(db, org)
    if remaining <= 0:
        raise HTTPException(402, {"error": {"code": "budget_exceeded",
                                            "message": "Monthly gateway budget exhausted for this org."}})

    chain = _resolve_order(db, org, request.model)
    if team is not None:
        allowed = team.allowed_models or ["*"]
        if "*" not in allowed:
            chain = [model for model in chain if model.slug in allowed]
            if not chain:
                raise HTTPException(403, {"error": {"code": "model_not_allowed_for_team",
                                                    "message": (f"No approved model for team '{team.name}' "
                                                                f"in the resolved routing chain.")}})

    # ---- DLP on outbound model traffic (same rule engine as agent traffic) ------
    from ..workforce import dlp_scan_outbound
    dlp_actions: list[dict] = []
    prompt_text = "\n".join(m.content for m in request.messages)
    scanned_text, violations, blocked = dlp_scan_outbound(db, org, prompt_text)
    if violations:
        dlp_actions = violations
    if blocked:
        audit(db, event_type="GATEWAY_DLP_BLOCKED", org_id=org.id, actor=f"key:{key.name}",
              description=f"{len(violations)} violation(s) blocked before model dispatch")
        raise HTTPException(403, {"error": {"code": "dlp_blocked",
                                            "message": "Request blocked by a data-loss-prevention rule "
                                                       "before it reached the model.",
                                            "violations": violations}})
    if any(v.get("action") == "REDACT" for v in violations):
        audit(db, event_type="GATEWAY_DLP_REDACTED", org_id=org.id, actor=f"key:{key.name}",
              description=f"{len(violations)} match(es) redacted before model dispatch")
        request = request.model_copy(deep=True)
        for message in request.messages:
            scanned_message, _, _ = dlp_scan_outbound(db, org, message.content)
            message.content = scanned_message
        prompt_text = scanned_text

    attempts: list[dict] = []
    cost_credits = 0.0
    for model in chain:
        try:
            result = _invoke_model(db, org, key, model, request, prompt_text)
            cost_credits = result["credits"]
            attempts.append({"model": model.slug, "ok": True})
            break
        except Exception as error:  # noqa: BLE001
            attempts.append({"model": model.slug, "ok": False, "error": str(error)[:180]})
            continue
    else:
        raise HTTPException(502, {"error": {"code": "all_providers_failed", "attempts": attempts}})

    text = result["text"]

    # ---- DLP on the response side too: nothing leaves the gateway unscanned -----
    response_text, response_violations, _blocked = dlp_scan_outbound(db, org, text)
    if response_violations:
        text = response_text
        dlp_actions.extend(response_violations)
        audit(db, event_type="GATEWAY_DLP_RESPONSE_REDACTED", org_id=org.id, actor=f"key:{key.name}",
              description=f"{len(response_violations)} match(es) redacted from model output")

    usage_row_note = {"attempts": attempts}
    usage(db, plane="gateway", kind="llm_request", credit_type="tokens",
          units=0, credits=cost_credits, org_id=org.id, key_id=key.id,
          team_id=team.id if team is not None else None, meta=usage_row_note)
    audit(db, event_type="GATEWAY_LLM_CALL", description=f"{result['model_served']} (-{cost_credits:g} cr)"
          + (f" [team:{team.name}]" if team is not None else ""),
          org_id=org.id, actor=f"key:{key.name}")

    if request.stream:
        return _stream_response(result, request, org, cost_credits)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(now_ts()),
        "model": request.model or result["model_served"],
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": result["input_tokens"],
                  "completion_tokens": result["output_tokens"],
                  "total_tokens": result["input_tokens"] + result["output_tokens"]},
        "connexa": {"credits_charged": cost_credits, "attempts": attempts,
                    "team": team.name if team is not None else None,
                    "dlp": dlp_actions},
    }


def now_ts() -> float:
    import time
    return time.time()


def _invoke_model(db: Session, org, key, model: GatewayModel, request: ChatCompletionRequest,
                  prompt_text: str) -> dict:
    messages = [m.model_dump() for m in request.messages]
    if model.provider_key == "echo":
        text = providers.echo_generate(prompt_text, request.max_tokens)
        input_tokens = providers.estimate_tokens(prompt_text)
        output_tokens = providers.estimate_tokens(text)
    elif model.provider_key == "openai":
        from ..config import settings
        if not settings.openai_key:
            raise RuntimeError("no OPENAI_API_KEY configured")
        real = providers.openai_generate(model.slug, messages, request.max_tokens, settings.openai_key)
        text, input_tokens, output_tokens = (real["text"], real["input_tokens"], real["output_tokens"])
    elif model.provider_key == "anthropic":
        from ..config import settings
        if not settings.anthropic_key:
            raise RuntimeError("no ANTHROPIC_API_KEY configured")
        real = providers.anthropic_generate(model.slug.replace("anthropic/", ""),
                                            messages, request.max_tokens, settings.anthropic_key)
        text, input_tokens, output_tokens = (real["text"], real["input_tokens"], real["output_tokens"])
    else:
        raise RuntimeError(f"unknown provider {model.provider_key}")
    credits = round((input_tokens / 1000.0) * model.input_cpt + (output_tokens / 1000.0) * model.output_cpt, 4)
    return {"text": text, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "credits": credits, "model_served": model.slug}


def _stream_response(result: dict, request: ChatCompletionRequest, org, cost_credits: float):
    """Emit an OpenAI-style delta stream (chunked re-emission of the completed text)."""
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]

    def generator():
        text = result["text"]
        words = text.split(" ") + [" "]
        buffer = ""
        for word in words:
            buffer += word + " " if not word.endswith(" ") else word
            chunk = {
                "id": completion_id, "object": "chat.completion.chunk", "created": int(now_ts()),
                "model": request.model or result["model_served"],
                "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}]}
            yield f"data: {json.dumps(chunk)}\n\n"
        final = {
            "id": completion_id, "object": "chat.completion.chunk", "created": int(now_ts()),
            "model": request.model or result["model_served"],
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        final_line = json.dumps(final)
        yield f"data: {final_line}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")

# ---------------------------------------------------------------- /v1/responses (light)


class ResponsesRequest(BaseModel):
    model: str | None = None
    input: str
    max_tokens: int = 256


@router.post("/responses")
def responses_api(request: ResponsesRequest, auth=Depends(gateway_auth), db: Session = Depends(get_db)):
    key, org = auth
    messages = [ChatMessage(role="user", content=request.input)]
    completion = chat_completions(
        ChatCompletionRequest(model=request.model, messages=messages,
                              max_tokens=request.max_tokens, stream=False),
        auth=auth, db=db)
    return {"id": completion["id"], "object": "response", "model": completion["model"],
            "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": completion["choices"][0]["message"]["content"]}]}],
            "usage": completion["usage"]}

# ---------------------------------------------------------------- budgets + usage

@router.get("/budget")
def get_budget(auth=Depends(gateway_auth), db: Session = Depends(get_db)):
    _key, org = auth
    budget = db.query(Budget).filter(Budget.org_id == org.id).first()
    if not budget:
        budget = Budget(id=uid(), org_id=org.id, credits_total=10_000)
        db.add(budget)
        db.commit()
    used = budget.credits_total - _remaining_budget(db, org)
    return {"period": budget.period, "credits_total": budget.credits_total,
            "credits_used_month": round(used, 2),
            "credits_remaining": _remaining_budget(db, org)}


@router.get("/usage")
def usage_report(auth=Depends(gateway_auth), db: Session = Depends(get_db)):
    from ..models import UsageLog
    _key, org = auth
    rows = (db.query(UsageLog).filter(UsageLog.org_id == org.id)
            .order_by(UsageLog.ts.desc()).limit(50).all())
    by_plane: dict[str, float] = {}
    for row in rows:
        by_plane[row.plane] = round(by_plane.get(row.plane, 0.0) + row.credits, 2)
    return {"budget_view": get_budget(auth=auth, db=db), "credits_by_plane": by_plane,
            "recent": [{"ts": iso(row.ts), "plane": row.plane, "kind": row.kind,
                        "credit_type": row.credit_type, "credits": row.credits} for row in rows]}
