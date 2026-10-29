from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from app.core.security import DB, Actor
from app.permissions.resources import entitled, visible_users
from app.persistence.models import AuditEvent, Click, Conversion, Offer, TrackingLink

router = APIRouter()


@router.get("/audit-events")
def list_audit(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    if user.role not in {"admin", "platform_admin"}:
        raise HTTPException(403, "Admin access required")
    query = (
        select(AuditEvent)
        .where(AuditEvent.workspace_id == user.workspace_id)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
        .offset(offset)
        .limit(limit)
    )
    return [
        {
            k: getattr(row, k)
            for k in ("id", "actor_id", "action", "resource_id", "detail", "created_at")
        }
        for row in db.scalars(query)
    ]


@router.get("/reports/summary")
def summary(db: DB, user: Actor, start: datetime | None = None, end: datetime | None = None):
    entitled(db, user)
    if user.role not in {"admin", "staff", "affiliate", "publisher", "advertiser"}:
        raise HTTPException(403, "Reporting access required")
    if (start and start.tzinfo is None) or (end and end.tzinfo is None):
        raise HTTPException(422, "Dates must include a timezone")
    if start and end and start >= end:
        raise HTTPException(422, "Start must precede end")
    clicks = select(func.count(Click.id)).where(Click.workspace_id == user.workspace_id)
    conversions = select(
        Conversion.currency,
        func.count(Conversion.id),
        func.coalesce(func.sum(Conversion.revenue), 0),
        func.coalesce(func.sum(Conversion.payout), 0),
    ).where(Conversion.workspace_id == user.workspace_id, Conversion.status == "approved")
    if user.role in {"affiliate", "publisher"}:
        clicks = clicks.join(TrackingLink, Click.link_id == TrackingLink.id).where(
            TrackingLink.affiliate_id.in_(visible_users(user))
        )
        conversions = (
            conversions.join(Click, Conversion.click_id == Click.id)
            .join(TrackingLink, Click.link_id == TrackingLink.id)
            .where(TrackingLink.affiliate_id.in_(visible_users(user)))
        )
    elif user.role == "advertiser":
        clicks = (
            clicks.join(TrackingLink, Click.link_id == TrackingLink.id)
            .join(Offer, TrackingLink.offer_id == Offer.id)
            .where(Offer.advertiser_id == user.id)
        )
        conversions = conversions.join(Offer, Conversion.offer_id == Offer.id).where(
            Offer.advertiser_id == user.id
        )
    for boundary, operator in ((start, "__ge__"), (end, "__lt__")):
        if boundary:
            clicks = clicks.where(getattr(Click.created_at, operator)(boundary))
            conversions = conversions.where(getattr(Conversion.created_at, operator)(boundary))
    totals = []
    for currency, count, revenue, payout in db.execute(conversions.group_by(Conversion.currency)):
        item = {"currency": currency, "approved_conversions": count, "payout": str(payout)}
        if user.role == "advertiser":
            item.pop("payout")
            item["revenue"] = str(revenue)
        elif user.role not in {"affiliate", "publisher"}:
            item.update(revenue=str(revenue), profit=str(Decimal(revenue) - Decimal(payout)))
        totals.append(item)
    from app.features.reporting.router import Filters, scope
    from app.persistence.network_models import Impression

    report_filters = Filters()
    impression_query = scope(
        select(func.count(Impression.id))
        .join(TrackingLink, TrackingLink.id == Impression.link_id)
        .join(Offer, Offer.id == TrackingLink.offer_id),
        user,
        report_filters,
        Impression,
    )
    unique_query = scope(
        select(func.count(func.distinct(Conversion.click_id)))
        .join(Click, Click.id == Conversion.click_id)
        .join(TrackingLink, TrackingLink.id == Click.link_id)
        .join(Offer, Offer.id == TrackingLink.offer_id)
        .where(Conversion.status == "approved"),
        user,
        report_filters,
    )
    for boundary, operator in ((start, "__ge__"), (end, "__lt__")):
        if boundary:
            impression_query = impression_query.where(
                getattr(Impression.created_at, operator)(boundary)
            )
            unique_query = unique_query.where(getattr(Conversion.created_at, operator)(boundary))
    return {
        "clicks": db.scalar(clicks),
        "impressions": db.scalar(impression_query),
        "approved_unique_clicks": db.scalar(unique_query),
        "totals": totals,
    }
