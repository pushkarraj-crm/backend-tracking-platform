import hashlib
import hmac
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, quote
import httpx
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from pydantic import Field
from .config import get_settings
from .models import Workspace, BillingEvent
from .schemas import StrictModel
from .security import DB, Actor
from .services import audit, enqueue
from .services import utc
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/billing", tags=["Billing"])
settings = get_settings()


class Subscribe(StrictModel):
    plan: str = Field(min_length=1, max_length=20)


def provider_request(method, path, body=None):
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        raise HTTPException(503, "Subscription billing is not configured")
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.request(
                method,
                "https://api.razorpay.com/v1/" + path,
                auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
                json=body,
            )
        if response.status_code >= 400:
            raise HTTPException(502, "Billing provider rejected the request")
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Billing provider unavailable")


@router.get("/plans")
def plans(user: Actor):
    return [
        {"id": plan, "limits": settings.plan_limits[plan]}
        for plan in settings.razorpay_plans
        if plan in settings.plan_limits
    ]


@router.post("/subscribe")
def subscribe(body: Subscribe, db: DB, user: Actor):
    if user.role != "admin":
        raise HTTPException(403, "Workspace admin required")
    plan_id = settings.razorpay_plans.get(body.plan)
    if not plan_id or body.plan not in settings.plan_limits or body.plan == "trial":
        raise HTTPException(422, "Plan unavailable")
    workspace = db.scalar(
        select(Workspace).where(Workspace.id == user.workspace_id).with_for_update()
    )
    # Existing subscriptions are reused, preventing parallel checkout creation.
    if workspace.razorpay_subscription_id:
        subscription = provider_request(
            "GET", "subscriptions/" + workspace.razorpay_subscription_id
        )
        if subscription["status"] not in {"cancelled", "completed", "expired"}:
            return checkout_response(subscription)
    payload = {
        "plan_id": plan_id,
        "total_count": 120,
        "quantity": 1,
        "customer_notify": 1,
        "notes": {"workspace_id": workspace.id},
    }
    # Signup does not require a card; schedule the first subscription cycle after the free trial.
    if workspace.trial_ends_at and utc(workspace.trial_ends_at) > datetime.now(timezone.utc):
        payload["start_at"] = int(utc(workspace.trial_ends_at).timestamp())
    subscription = provider_request("POST", "subscriptions", payload)
    workspace.razorpay_subscription_id = subscription["id"]
    audit(db, user, "billing.subscription_created", workspace.id, body.plan)
    db.commit()
    return checkout_response(subscription)


def checkout_response(subscription):
    url = subscription.get("short_url", "")
    if urlsplit(url).scheme != "https":
        raise HTTPException(502, "Invalid provider checkout URL")
    return {"checkout_url": url, "subscription_id": subscription["id"]}


@router.post("/cancel")
def cancel(db: DB, user: Actor):
    if user.role != "admin":
        raise HTTPException(403, "Workspace admin required")
    workspace = db.get(Workspace, user.workspace_id)
    if not workspace.razorpay_subscription_id:
        raise HTTPException(404, "No subscription")
    provider_request(
        "POST",
        "subscriptions/" + workspace.razorpay_subscription_id + "/cancel",
        {"cancel_at_cycle_end": 1},
    )
    audit(db, user, "billing.cancellation_requested", workspace.id)
    db.commit()
    return {"cancellation_scheduled": True}


@router.post("/webhook")
async def webhook(request: Request, db: DB):
    if not settings.razorpay_webhook_secret:
        raise HTTPException(503, "Billing webhook is not configured")
    raw = await request.body()
    return await run_in_threadpool(ingest_subscription_webhook, raw, dict(request.headers), db)


def ingest_subscription_webhook(raw, headers, db):
    signature = headers.get("x-razorpay-signature", "")
    expected = hmac.new(settings.razorpay_webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "Invalid webhook signature")
    event_id = headers.get("x-razorpay-event-id", "")
    if not event_id or len(event_id) > 100:
        raise HTTPException(422, "Webhook event ID required")
    if db.get(BillingEvent, event_id):
        return {"received": True, "duplicate": True}
    try:
        data = json.loads(raw)
        subscription_id = data["payload"]["subscription"]["entity"]["id"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "Subscription event required")
    workspace = db.scalar(
        select(Workspace).where(Workspace.razorpay_subscription_id == subscription_id)
    )
    if not workspace:
        raise HTTPException(404, "Unknown subscription")
    db.add(BillingEvent(id=event_id))
    enqueue(db, workspace.id, "billing", {"subscription_id": subscription_id})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"received": True, "duplicate": True}
    return {"received": True}


def reconcile_subscription(db, workspace, subscription_id):
    # Fetch current provider state; delayed/out-of-order webhooks cannot restore cancelled access.
    state = provider_request("GET", "subscriptions/" + subscription_id)
    if workspace.razorpay_subscription_id != state["id"]:
        return
    plan = next(
        (
            name
            for name, identifier in settings.razorpay_plans.items()
            if identifier == state["plan_id"]
        ),
        None,
    )
    if not plan:
        raise RuntimeError("Unknown provider plan")
    if state["status"] == "active" and state.get("current_end"):
        workspace.plan = plan
        workspace.subscription_status = "active"
        workspace.paid_until = datetime.fromtimestamp(state["current_end"], timezone.utc)
        workspace.billing_grace_until = None
    elif state["status"] in {"cancelled", "completed", "expired", "halted", "paused", "pending"}:
        if (
            state["status"] == "pending"
            and workspace.subscription_status != "pending"
            and workspace.paid_until
        ):
            workspace.billing_grace_until = utc(workspace.paid_until) + timedelta(
                hours=settings.billing_grace_hours
            )
        workspace.subscription_status = state["status"]


@router.post("/pause")
def pause(db: DB, user: Actor):
    return subscription_action(db, user, "pause", {"pause_at": "now"})


@router.post("/resume")
def resume(db: DB, user: Actor):
    return subscription_action(db, user, "resume", {"resume_at": "now"})


def subscription_action(db, user, action, payload):
    if user.role != "admin":
        raise HTTPException(403, "Workspace admin required")
    workspace = db.scalar(
        select(Workspace).where(Workspace.id == user.workspace_id).with_for_update()
    )
    if not workspace.razorpay_subscription_id:
        raise HTTPException(404, "No subscription")
    provider_request(
        "POST", "subscriptions/" + workspace.razorpay_subscription_id + "/" + action, payload
    )
    reconcile_subscription(db, workspace, workspace.razorpay_subscription_id)
    audit(db, user, "billing." + action, workspace.id)
    db.commit()
    return {"status": workspace.subscription_status}


@router.post("/reconcile")
def reconcile(db: DB, user: Actor):
    if user.role != "admin":
        raise HTTPException(403, "Workspace admin required")
    workspace = db.scalar(
        select(Workspace).where(Workspace.id == user.workspace_id).with_for_update()
    )
    if not workspace.razorpay_subscription_id:
        raise HTTPException(404, "No subscription")
    reconcile_subscription(db, workspace, workspace.razorpay_subscription_id)
    db.commit()
    return {
        "status": workspace.subscription_status,
        "paid_until": workspace.paid_until,
        "grace_until": workspace.billing_grace_until,
    }


@router.get("/invoices")
def invoices(db: DB, user: Actor):
    if user.role != "admin":
        raise HTTPException(403, "Workspace admin required")
    workspace = db.get(Workspace, user.workspace_id)
    if not workspace.razorpay_subscription_id:
        return {"items": []}
    return provider_request(
        "GET", "invoices?subscription_id=" + quote(workspace.razorpay_subscription_id, safe="")
    )
