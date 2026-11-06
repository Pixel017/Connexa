"""Gateway plane: model catalog, routing policies, OpenAI-compatible chat completions,
budget enforcement, and usage metering — the LLM routing face of the platform.
"""
from __future__ import annotations

import itertools
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Budget, GatewayModel, RoutingPolicy, Org, ApiKey
from ..metering import audit, check_gateway_budget, usage
from ..utils import uid, iso, now
from . import providers

router = APIRouter(prefix="/v1")


def gateway_key(db_key_dep):
    return db_key_dep


@router.get("/models")
def list_models(auth=Depends(gateway_auth), db: Session = Depends(get_db)):
    _, org = auth
    rows = db.query(GatewayModel).filter(GatewayModel.status != "retired").all()
    return {"object": "list", "data": [
        {"id": r.slug, "vendor": r.vendor, "family": r.family, "status": r.status,
         "context_window": r.context_window,
         "pricing_credits_per_1k": {"input": r.input_cpt, "output": r.output_cpt},
         "capabilities": r.capabilities} for r in rows]}


@router.get("/vendors")
def list_vendors(auth=Depends(gateway_auth), db: Session = Depends(get_db)):
    _, org = auth
    rows = db.query(GatewayModel).all()
    vendors: dict[str, dict] = {}
    for row in rows:
        entry = vendors.setdefault(row.vendor, {"vendor": row.vendor, "models": 0,
                                                "families": set()})
        entry["models"] += 1
        entry["families"].add(row.family)
    for entry in vendors.values():
        entry["families"] = sorted(entry["families"])
    return {"count": len(vendors), "vendors": sorted(vendors.values(), key=lambda v: v["vendor"])}
