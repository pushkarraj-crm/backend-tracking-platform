from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field
from sqlalchemy import func, select

from app.core.security import DB, Actor
from app.core.services import audit, entitlement, utc
from app.features.accounts.notifications import notify, notify_admins
from app.permissions.campaigns import manage_campaign
from app.permissions.employees import employee_capability
from app.permissions.resources import owned
from app.persistence.models import (
    Click,
    Conversion,
    Offer,
    TrackingLink,
    User,
    Workspace,
)
from app.persistence.network_models import AutomationLog, AutomationRule
from app.shared.schemas import ActiveUpdate, StrictModel

router = APIRouter(tags=["Network tools"])


class AutomationBody(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    offer_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    lookback_hours: int = Field(default=24, ge=1, le=168)
    minimum_clicks: int = Field(default=100, ge=1, le=10000000)
    metric: Literal["clicks", "approved_conversions", "conversion_rate"] = "conversion_rate"
    comparison: Literal["lt", "gt"] = "lt"
    threshold: Decimal = Field(ge=0, max_digits=14, decimal_places=4)
    action: Literal["notify", "pause"] = "notify"
    cooldown_hours: int = Field(default=24, ge=1, le=168)


@router.post("/tools/automation", status_code=201)
def create_automation(body: AutomationBody, db: DB, user: Actor):
    manage_campaign(db, user, body.offer_id, lock=True)
    employee_capability(user, "campaigns:write")
    if (
        db.scalar(
            select(func.count())
            .select_from(AutomationRule)
            .where(
                AutomationRule.workspace_id == user.workspace_id,
                AutomationRule.offer_id == body.offer_id,
            )
        )
        >= 20
    ):
        raise HTTPException(409, "Automation limit reached")
    row = AutomationRule(
        workspace_id=user.workspace_id,
        offer_id=body.offer_id,
        user_id=user.id,
        name=body.name,
        configuration=body.model_dump(mode="json", exclude={"name", "offer_id"}),
    )
    db.add(row)
    db.flush()
    audit(db, user, "automation.created", row.id)
    db.commit()
    return automation_output(row)


def automation_output(row):
    return {
        "id": row.id,
        "name": row.name,
        "offer_id": row.offer_id,
        "active": row.active,
        "next_check_at": row.next_check_at,
        "last_triggered_at": row.last_triggered_at,
        **row.configuration,
    }


@router.get("/tools/automation")
def automations(db: DB, user: Actor):
    query = (
        select(AutomationRule)
        .join(Offer, Offer.id == AutomationRule.offer_id)
        .where(AutomationRule.workspace_id == user.workspace_id)
    )
    if user.role == "advertiser":
        query = query.where(Offer.advertiser_id == user.id)
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Campaign management role required")
    return [automation_output(r) for r in db.scalars(query.order_by(AutomationRule.id).limit(100))]


@router.patch("/tools/automation/{rule_id}")
def automation_active(rule_id: str, body: ActiveUpdate, db: DB, user: Actor):
    row = owned(db, AutomationRule, rule_id, user)
    manage_campaign(db, user, row.offer_id)
    employee_capability(user, "campaigns:write")
    row.active = body.active
    audit(db, user, "automation.active", row.id, str(body.active))
    db.commit()
    return automation_output(row)


@router.get("/tools/automation-logs")
def automation_logs(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    query = (
        select(AutomationLog)
        .join(AutomationRule, AutomationRule.id == AutomationLog.rule_id)
        .join(Offer, Offer.id == AutomationRule.offer_id)
        .where(AutomationLog.workspace_id == user.workspace_id)
    )
    if user.role == "advertiser":
        query = query.where(Offer.advertiser_id == user.id)
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Campaign management role required")
    return [
        {k: getattr(r, k) for k in ["id", "rule_id", "action", "observed", "created_at"]}
        for r in db.scalars(
            query.order_by(AutomationLog.created_at.desc(), AutomationLog.id)
            .offset(offset)
            .limit(limit)
        )
    ]


def run_automations(db):
    at = datetime.now(timezone.utc)
    rows = list(
        db.scalars(
            select(AutomationRule)
            .where(AutomationRule.active.is_(True), AutomationRule.next_check_at <= at)
            .order_by(AutomationRule.next_check_at, AutomationRule.id)
            .with_for_update(skip_locked=True)
            .limit(100)
        )
    )
    for row in rows:
        row.next_check_at = at + timedelta(hours=1)
        config = AutomationBody(name=row.name, offer_id=row.offer_id, **row.configuration)
        if row.last_triggered_at and utc(row.last_triggered_at) > at - timedelta(
            hours=config.cooldown_hours
        ):
            continue
        offer = db.scalar(
            select(Offer)
            .where(Offer.id == row.offer_id, Offer.workspace_id == row.workspace_id)
            .with_for_update()
        )
        creator = db.get(User, row.user_id)
        workspace = db.get(Workspace, row.workspace_id)
        if not creator or not creator.active or not workspace.active or not offer.active:
            continue
        if (
            creator.role == "staff"
            and creator.staff_permissions is not None
            and "campaigns:write" not in creator.staff_permissions
        ):
            continue
        try:
            entitlement(workspace)
        except HTTPException:
            continue
        start = at - timedelta(hours=config.lookback_hours)
        clicks = db.scalar(
            select(func.count(Click.id))
            .join(TrackingLink, TrackingLink.id == Click.link_id)
            .where(
                Click.workspace_id == row.workspace_id,
                TrackingLink.offer_id == offer.id,
                Click.created_at >= start,
                Click.created_at < at,
            )
        )
        if clicks < config.minimum_clicks:
            continue
        conversions = db.scalar(
            select(func.count(Conversion.id)).where(
                Conversion.workspace_id == row.workspace_id,
                Conversion.offer_id == offer.id,
                Conversion.status == "approved",
                Conversion.created_at >= start,
                Conversion.created_at < at,
            )
        )
        unique = db.scalar(
            select(func.count(func.distinct(Conversion.click_id)))
            .join(Click, Click.id == Conversion.click_id)
            .where(
                Conversion.workspace_id == row.workspace_id,
                Conversion.offer_id == offer.id,
                Conversion.status == "approved",
                Click.created_at >= start,
                Click.created_at < at,
            )
        )
        values = {
            "clicks": Decimal(clicks),
            "approved_conversions": Decimal(conversions),
            "conversion_rate": Decimal(unique) * 100 / Decimal(clicks),
        }
        value = values[config.metric]
        triggered = (
            value < config.threshold if config.comparison == "lt" else value > config.threshold
        )
        if not triggered:
            continue
        if config.action == "pause":
            offer.active = False
        row.last_triggered_at = at
        db.add(
            AutomationLog(
                workspace_id=row.workspace_id,
                rule_id=row.id,
                action=config.action,
                observed={k: str(v) for k, v in values.items()},
            )
        )
        audit(db, creator, "automation.triggered", offer.id, config.action)
        notify(
            db,
            row.workspace_id,
            [row.user_id, offer.advertiser_id],
            "automation.triggered",
            "Campaign automation triggered",
            row.name,
            offer.id,
        )
        notify_admins(
            db,
            row.workspace_id,
            "automation.triggered",
            "Campaign automation triggered",
            row.name,
            offer.id,
        )
