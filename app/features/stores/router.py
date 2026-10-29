"""Online-store sale tracking from the browser: a public account key and always-pending sales."""

import json
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.security import DB, Actor
from app.core.services import audit
from app.features.conversions.router import record_values
from app.permissions.roles import staff_only
from app.persistence.models import Workspace

router = APIRouter(tags=["Store tracking"])
FIELDS = {
    "click_id",
    "coupon",
    "txn_id",
    "amount",
    "currency",
    "event",
    *[f"adv_sub{i}" for i in range(1, 6)],
}


def cors(request):
    # Store pages send text/plain (a "simple" request, so no preflight); nothing here is
    # secret and no cookies are used, so any page may read the reply.
    return {
        "Access-Control-Allow-Origin": request.headers.get("origin") or "*",
        "Vary": "Origin",
        "Cache-Control": "no-store",
    }


@router.post("/wc/{key}")
async def browser_conversion(key: str, request: Request, db: DB):
    """A sale reported by a store page. It is always recorded as pending until confirmed."""
    try:
        data = json.loads((await request.body()) or b"{}")
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return JSONResponse({"detail": "Send a JSON object"}, 422, headers=cors(request))
    values = {
        str(k).lower(): str(v).strip()
        for k, v in data.items()
        if str(k).lower() in FIELDS and v not in (None, "")
    }
    if not values.get("txn_id"):
        return JSONResponse(
            {"detail": "txn_id (the order ID) is required"}, 422, headers=cors(request)
        )
    try:
        result = await run_in_threadpool(receive, db, key, values, request)
    except HTTPException as error:
        return JSONResponse({"detail": error.detail}, error.status_code, headers=cors(request))
    if isinstance(result, JSONResponse):  # a repeated order
        return JSONResponse(json.loads(result.body), result.status_code, headers=cors(request))
    return JSONResponse(result, 201, headers=cors(request))


def receive(db, key, values, request):
    workspace = (
        db.scalar(select(Workspace).where(Workspace.web_key == key))
        if 16 <= len(key) <= 64
        else None
    )
    if not workspace:
        raise HTTPException(401, "Unknown store key")
    return record_values(db, workspace, values, request, browser=True)


@router.get("/web-conversion-settings")
def web_conversion_settings(db: DB, user: Actor):
    from app.tenancy.service import company_context

    staff_only(user)
    workspace = db.get(Workspace, user.workspace_id)
    if not workspace.web_key:
        workspace.web_key = secrets.token_urlsafe(18)
        audit(db, user, "web_key.created", workspace.id, "store tracking")
        db.commit()
    company = company_context.get()
    base = get_settings().public_base_url.rstrip("/")
    prefix = "/c/" + company.slug if company else ""
    return {
        "endpoint": f"{base}{prefix}/wc/{workspace.web_key}",
        "sdk_url": f"{base}/sdk/relay.js",
    }
