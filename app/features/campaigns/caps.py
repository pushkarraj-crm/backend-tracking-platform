"""Atomic gross caps. Counters include historical traffic at first initialization."""

from datetime import timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, or_, select, update

from app.core.services import utc
from app.features.tracking.rules import insert_once
from app.persistence.models import Click, Conversion, TrackingLink, identifier
from app.persistence.network_models import PeriodCap, PeriodCapCounter


def bucket(cap, at):
    local = utc(at).astimezone(ZoneInfo(cap.timezone))
    if cap.period == "total":
        return "total", None, None
    if cap.period == "hour":
        start = local.replace(minute=0, second=0, microsecond=0)
        # Repeated daylight-saving hours have distinct offsets and buckets.
        return (
            start.strftime("%Y%m%d%H%z"),
            start.astimezone(timezone.utc),
            start.astimezone(timezone.utc) + timedelta(hours=1),
        )
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if cap.period == "day":
        end = start + timedelta(days=1)
        key = start.strftime("%Y%m%d")
    elif cap.period == "week":
        start = start - timedelta(days=start.weekday())
        end = start + timedelta(days=7)
        key = start.strftime("%G-W%V")
    else:
        start = start.replace(day=1)
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
        key = start.strftime("%Y%m")
    return key, start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def historical_usage(db, cap, start, end):
    if cap.metric == "clicks":
        query = (
            select(func.count(Click.id))
            .join(TrackingLink, TrackingLink.id == Click.link_id)
            .where(Click.workspace_id == cap.workspace_id, TrackingLink.offer_id == cap.offer_id)
        )
        created = Click.created_at
    else:
        metric = (
            func.count(Conversion.id)
            if cap.metric == "conversions"
            else func.coalesce(func.sum(getattr(Conversion, cap.metric)), 0)
        )
        query = (
            select(metric)
            .join(Click, Click.id == Conversion.click_id)
            .join(TrackingLink, TrackingLink.id == Click.link_id)
            .where(Conversion.workspace_id == cap.workspace_id, Conversion.offer_id == cap.offer_id)
        )
        created = Conversion.created_at
        if cap.event_key:
            query = query.where(Conversion.event_key == cap.event_key)
    if cap.publisher_id:
        query = query.where(TrackingLink.affiliate_id == cap.publisher_id)
    if start:
        query = query.where(created >= start, created < end)
    return Decimal(db.scalar(query) or 0)


def reserve(
    db, offer, publisher_id, kind, at, event_key="initial", revenue=Decimal(0), payout=Decimal(0)
):
    metrics = ["clicks"] if kind == "click" else ["conversions", "revenue", "payout"]
    caps = list(
        db.scalars(
            select(PeriodCap)
            .where(
                PeriodCap.workspace_id == offer.workspace_id,
                PeriodCap.offer_id == offer.id,
                PeriodCap.active.is_(True),
                PeriodCap.metric.in_(metrics),
                or_(PeriodCap.publisher_id.is_(None), PeriodCap.publisher_id == publisher_id),
                or_(PeriodCap.event_key.is_(None), PeriodCap.event_key == event_key),
            )
            .order_by(PeriodCap.id)
            .limit(100)
        )
    )
    for cap in caps:
        key, start, end = bucket(cap, at)
        existing = db.scalar(
            select(PeriodCapCounter.id).where(
                PeriodCapCounter.workspace_id == offer.workspace_id,
                PeriodCapCounter.cap_id == cap.id,
                PeriodCapCounter.bucket == key,
            )
        )
        if not existing:
            initial = historical_usage(db, cap, start, end)
            insert_once(
                db,
                PeriodCapCounter,
                {
                    "id": identifier(),
                    "workspace_id": offer.workspace_id,
                    "cap_id": cap.id,
                    "bucket": key,
                    "amount": initial,
                },
                ["workspace_id", "cap_id", "bucket"],
            )
        delta = (
            Decimal(1)
            if cap.metric in {"clicks", "conversions"}
            else Decimal(revenue if cap.metric == "revenue" else payout)
        )
        current = db.scalar(
            update(PeriodCapCounter)
            .where(
                PeriodCapCounter.workspace_id == offer.workspace_id,
                PeriodCapCounter.cap_id == cap.id,
                PeriodCapCounter.bucket == key,
                PeriodCapCounter.amount + delta <= cap.amount,
            )
            .values(amount=PeriodCapCounter.amount + delta)
            .returning(PeriodCapCounter.amount)
        )
        if current is None:
            raise HTTPException(429, "Campaign period cap reached")
        from app.features.campaigns.policy import policy

        threshold = cap.amount * Decimal(policy(offer).cap_alert_percent) / 100
        if delta > 0 and current >= threshold and current - delta < threshold:
            from app.features.accounts.notifications import notify

            targets = [offer.advertiser_id] if offer.advertiser_id else []
            if cap.publisher_id:
                targets.append(cap.publisher_id)
            notify(
                db,
                offer.workspace_id,
                targets,
                "campaign.cap_threshold",
                "Campaign cap threshold reached",
                f"{cap.name}: {current} of {cap.amount} ({policy(offer).cap_alert_percent}% threshold).",
                offer.id,
            )
