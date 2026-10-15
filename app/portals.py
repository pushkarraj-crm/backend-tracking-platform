import secrets
from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, HttpUrl
from sqlalchemy import select, func
from .schemas import StrictModel
from .security import DB, Actor
from .models import Workspace, User, WebhookEndpoint, OutboxEvent
from .services import audit, encrypt, entitlement
from .routes import owned
from .delivery import validate_destination

router = APIRouter(tags=["Portals"])


class WebhookBody(StrictModel):
    affiliate_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    url: HttpUrl


class WorkspaceUpdate(StrictModel):
    active: bool


def platform_only(user):
    if user.role != "platform_admin":
        raise HTTPException(403, "Platform owner access required")


def workspace_output(workspace):
    return {
        k: getattr(workspace, k)
        for k in (
            "id",
            "name",
            "active",
            "plan",
            "subscription_status",
            "trial_ends_at",
            "paid_until",
            "created_at",
        )
    }


@router.get("/workspace")
def workspace_info(db: DB, user: Actor):
    workspace = db.get(Workspace, user.workspace_id)
    result = workspace_output(workspace)
    if user.role in {"admin", "staff"}:
        result["profile"] = {
            k: getattr(workspace, k)
            for k in (
                "business_type",
                "usage_types",
                "country",
                "mobile",
                "teams_enabled",
                "teams_email",
                "preferred_language",
                "referral_source",
            )
        }
    return result


@router.get("/platform/summary")
def platform_summary(db: DB, user: Actor):
    platform_only(user)
    return {
        "workspaces": db.scalar(select(func.count()).select_from(Workspace)),
        "users": db.scalar(select(func.count()).select_from(User)),
        "active_subscriptions": db.scalar(
            select(func.count())
            .select_from(Workspace)
            .where(Workspace.subscription_status == "active")
        ),
        "pending_deliveries": db.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status.in_(["pending", "processing"]))
        ),
    }


@router.get("/platform/workspaces")
def platform_workspaces(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    platform_only(user)
    return [
        workspace_output(w)
        for w in db.scalars(
            select(Workspace)
            .order_by(Workspace.created_at.desc(), Workspace.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@router.patch("/platform/workspaces/{workspace_id}")
def platform_workspace_update(workspace_id: str, body: WorkspaceUpdate, db: DB, user: Actor):
    platform_only(user)
    if workspace_id == user.workspace_id:
        raise HTTPException(400, "Cannot suspend platform owner workspace")
    workspace = db.get(Workspace, workspace_id)
    if not workspace:
        raise HTTPException(404, "Workspace not found")
    workspace.active = body.active
    audit(db, user, "platform.workspace_active", workspace.id, str(body.active))
    db.commit()
    return workspace_output(workspace)


@router.put("/webhooks")
def configure_webhook(body: WebhookBody, db: DB, user: Actor):
    entitlement(db.get(Workspace, user.workspace_id))
    affiliate = owned(db, User, body.affiliate_id, user)
    if affiliate.role not in {"affiliate", "publisher"}:
        raise HTTPException(400, "Affiliate or publisher required")
    if user.role not in {"admin", "staff"} and user.id != affiliate.id:
        raise HTTPException(403, "Can configure only your own webhook")
    validate_destination(str(body.url))
    endpoint = db.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.workspace_id == user.workspace_id,
            WebhookEndpoint.affiliate_id == affiliate.id,
        )
    )
    raw = secrets.token_urlsafe(48)
    if endpoint is None:
        endpoint = WebhookEndpoint(workspace_id=user.workspace_id, affiliate_id=affiliate.id)
        db.add(endpoint)
    endpoint.url = str(body.url)
    endpoint.secret = encrypt(raw)
    endpoint.active = True
    db.flush()
    audit(db, user, "webhook.configured", endpoint.id)
    db.commit()
    return {"id": endpoint.id, "secret": raw, "url": endpoint.url}


@router.get("/deliveries")
def deliveries(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    if user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Staff access required")
    return [
        {k: getattr(e, k) for k in ("id", "kind", "status", "attempts", "last_error", "created_at")}
        for e in db.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.workspace_id == user.workspace_id)
            .order_by(OutboxEvent.created_at.desc(), OutboxEvent.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@router.post("/deliveries/{event_id}/retry")
def retry_delivery(event_id: str, db: DB, user: Actor):
    if user.role != "admin":
        raise HTTPException(403, "Admin access required")
    event = owned(db, OutboxEvent, event_id, user)
    if event.status != "failed":
        raise HTTPException(409, "Only failed deliveries can be retried")
    event.status = "pending"
    event.attempts = 0
    from .models import now

    event.available_at = now()
    audit(db, user, "delivery.retried", event.id)
    db.commit()
    return {"queued": True}
