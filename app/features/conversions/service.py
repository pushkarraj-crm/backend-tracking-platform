"""Attributed event ingestion and ledger transitions shared by S2S and operator tools."""

from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.core.services import audit, enqueue, entitlement, utc
from app.features.campaigns.policy import matches, policy, rule_values
from app.persistence.models import (
    Click,
    CommissionEntry,
    Conversion,
    InvoiceReservation,
    TrackingLink,
    User,
    WebhookEndpoint,
    Workspace,
)
from app.persistence.network_models import CampaignEvent, CommissionRule, TrafficRule


def same_payload(existing, body):
    return (
        existing.click_id == body.click_id
        and existing.sale_amount == body.sale_amount
        and existing.event_type == body.event_type
        and existing.advertiser_parameters == body.advertiser_parameters
    )


def existing_conversion(db, offer, body):
    return db.scalar(
        select(Conversion).where(
            Conversion.workspace_id == offer.workspace_id,
            Conversion.offer_id == offer.id,
            Conversion.event_key == body.event_key,
            Conversion.transaction_id == body.transaction_id,
        )
    )


def ingest(db, offer, body, offline=False, browser=False):
    from app.features.campaigns.caps import reserve as reserve_period_caps
    from app.features.tracking.rules import Rules, reject_fraud, reserve

    if body.sale_currency and body.sale_currency != offer.currency:
        raise HTTPException(422, "Sale currency does not match the campaign")
    click = db.scalar(
        select(Click)
        .join(TrackingLink, TrackingLink.id == Click.link_id)
        .where(
            Click.id == body.click_id,
            Click.workspace_id == offer.workspace_id,
            TrackingLink.offer_id == offer.id,
        )
        .with_for_update(of=Click)
    )
    if not click:
        raise HTTPException(404, "Click not found for this offer")
    existing = existing_conversion(db, offer, body)
    if existing:
        if not same_payload(existing, body):
            raise HTTPException(409, "Retry payload does not match the original conversion")
        return existing, True
    entitlement(db.get(Workspace, offer.workspace_id))
    # Offline advertiser reports (MIS) arrive weeks after the click and are the
    # advertiser's own confirmation, so window and speed checks apply to live postbacks only.
    if not offline and utc(click.created_at) < datetime.now(timezone.utc) - timedelta(
        hours=offer.attribution_hours
    ):
        raise HTTPException(422, "Click attribution window expired")
    rules = Rules.model_validate(offer.tracking_rules)
    if (
        not offline
        and (datetime.now(timezone.utc) - utc(click.created_at)).total_seconds()
        < rules.minimum_conversion_seconds
    ):
        reject_fraud(db, offer, "conversion_too_fast", 80, click.ip_hash, click.id)
    settings = policy(offer)
    event = None
    if body.event_key != "initial":
        event = db.scalar(
            select(CampaignEvent).where(
                CampaignEvent.workspace_id == offer.workspace_id,
                CampaignEvent.offer_id == offer.id,
                CampaignEvent.code == body.event_key,
                CampaignEvent.active.is_(True),
            )
        )
        if not event:
            raise HTTPException(422, "Unknown or inactive campaign event")
    repeatable = event.repeatable if event else settings.initial_repeatable
    if not repeatable and db.scalar(
        select(Conversion.id).where(
            Conversion.workspace_id == offer.workspace_id,
            Conversion.click_id == click.id,
            Conversion.event_key == body.event_key,
        )
    ):
        raise HTTPException(409, "This click already converted for this event")
    link = db.get(TrackingLink, click.link_id)
    values = rule_values(
        click, link.affiliate_id, body.event_key, body.sale_amount, body.advertiser_parameters
    )
    pricing = event or offer
    source = "event" if event else "campaign"
    for rule in db.scalars(
        select(CommissionRule)
        .where(
            CommissionRule.workspace_id == offer.workspace_id,
            CommissionRule.offer_id == offer.id,
            CommissionRule.active.is_(True),
            or_(
                CommissionRule.publisher_id.is_(None),
                CommissionRule.publisher_id == link.affiliate_id,
            ),
            or_(CommissionRule.event_key.is_(None), CommissionRule.event_key == body.event_key),
        )
        .order_by(CommissionRule.priority, CommissionRule.id)
        .limit(100)
    ):
        if matches(rule.conditions, values):
            pricing = rule
            source = "rule"
            break
    model = pricing.campaign_model if source == "campaign" else pricing.model
    revenue, payout = pricing.revenue, pricing.payout
    if model == "CPS":
        if body.event_type != "sale" or body.sale_amount is None:
            raise HTTPException(422, "CPS postbacks require event_type=sale and sale_amount")
        payout = (body.sale_amount * pricing.sale_percentage / Decimal(100)).quantize(
            Decimal("0.0001"), rounding=ROUND_DOWN
        )
        revenue = payout
    elif model == "CPL" and (body.event_type not in {None, "lead"} or body.sale_amount is not None):
        raise HTTPException(422, "CPL campaigns accept lead events only")
    state = (
        "approved"
        if (event.auto_approve if event else settings.initial_auto_approve)
        else "pending"
    )
    for rule in db.scalars(
        select(TrafficRule)
        .where(
            TrafficRule.workspace_id == offer.workspace_id,
            TrafficRule.offer_id == offer.id,
            TrafficRule.phase == "conversion",
            TrafficRule.active.is_(True),
        )
        .order_by(TrafficRule.priority, TrafficRule.id)
        .limit(100)
    ):
        if matches(rule.conditions, values):
            state = {"approve": "approved", "reject": "rejected", "pending": "pending"}[rule.action]
            break
    if browser and state == "approved":
        # Anyone can call a public browser endpoint, so its sales wait for confirmation.
        state = "pending"
    from app.features.sources.router import blocked_preset
    from app.persistence.models import FraudEvent

    blocked = blocked_preset(db, offer, click.attributes or {})
    if blocked:
        # The click predates the block; the conversion is kept for the record but earns nothing.
        state = "rejected"
        db.add(
            FraudEvent(
                workspace_id=offer.workspace_id,
                offer_id=offer.id,
                click_id=click.id,
                publisher_id=link.affiliate_id,
                ip_hash=click.ip_hash,
                reason="blocked_source:" + blocked,
                score=100,
            )
        )
    at = datetime.now(timezone.utc)
    reserve(db, offer, "conversions", at, revenue=revenue)
    reserve_period_caps(
        db, offer, link.affiliate_id, "conversion", at, body.event_key, revenue, payout
    )
    conversion = Conversion(
        workspace_id=offer.workspace_id,
        offer_id=offer.id,
        click_id=click.id,
        transaction_id=body.transaction_id,
        event_key=body.event_key,
        event_type=body.event_type,
        sale_amount=body.sale_amount,
        advertiser_parameters=body.advertiser_parameters,
        deduplication_key=body.transaction_id if repeatable else "",
        revenue=revenue,
        payout=payout,
        currency=offer.currency,
        created_at=at,
        pricing_snapshot={
            "source": source,
            "rule_id": pricing.id if source == "rule" else None,
            "event": body.event_key,
            "model": model,
            "percentage": str(pricing.sale_percentage) if model == "CPS" else None,
            "revenue": str(revenue),
            "payout": str(payout),
        },
    )
    db.add(conversion)
    db.flush()
    if state != "pending":
        transition(db, campaign_actor(db, offer), conversion, state)
    return conversion, False


def campaign_actor(db, offer):
    """The account that automated status changes are attributed to."""
    actor = (
        db.get(User, offer.advertiser_id)
        if offer.advertiser_id
        else db.scalar(
            select(User)
            .where(User.workspace_id == offer.workspace_id, User.role == "admin")
            .order_by(User.id)
        )
    )
    if not actor:
        raise HTTPException(409, "Campaign owner is unavailable")
    return actor


def approve_lead(db, user, offer, reference, event_key):
    """An approved later stage (e.g. disbursal) proves the lead; rejected leads stay rejected."""
    if event_key == "initial":
        return False
    lead = db.scalar(
        select(Conversion)
        .where(
            Conversion.workspace_id == offer.workspace_id,
            Conversion.offer_id == offer.id,
            Conversion.transaction_id == reference,
            Conversion.event_key == "initial",
            Conversion.status == "pending",
        )
        .with_for_update()
    )
    if lead:
        transition(db, user, lead, "approved")
    return bool(lead)


def transition(db, user, conversion, status):
    if conversion.status == status:
        return
    if db.get(InvoiceReservation, conversion.id):
        raise HTTPException(
            409, "Invoiced conversions cannot be changed; resolve the invoice first"
        )
    previous = conversion.status
    conversion.status = status
    conversion.version += 1
    link = db.scalar(
        select(TrackingLink)
        .join(Click, Click.link_id == TrackingLink.id)
        .where(Click.id == conversion.click_id)
    )
    db.scalar(select(User).where(User.id == link.affiliate_id).with_for_update())
    change = (
        conversion.payout
        if status == "approved"
        else -conversion.payout
        if previous == "approved"
        else None
    )
    if change is not None:
        db.add(
            CommissionEntry(
                workspace_id=user.workspace_id,
                affiliate_id=link.affiliate_id,
                conversion_id=conversion.id,
                version=conversion.version,
                amount=change,
                currency=conversion.currency,
            )
        )
    endpoint = db.scalar(
        select(WebhookEndpoint)
        .where(
            WebhookEndpoint.workspace_id == user.workspace_id,
            WebhookEndpoint.affiliate_id == link.affiliate_id,
            WebhookEndpoint.active.is_(True),
            or_(
                WebhookEndpoint.offer_id == conversion.offer_id, WebhookEndpoint.offer_id.is_(None)
            ),
        )
        .order_by(WebhookEndpoint.offer_id.is_not(None).desc(), WebhookEndpoint.id)
        .limit(1)
    )
    click = db.get(Click, conversion.click_id)
    if endpoint:
        enqueue(
            db,
            user.workspace_id,
            "webhook",
            {
                "endpoint_id": endpoint.id,
                "event": {
                    "conversion_id": conversion.id,
                    "campaign_id": conversion.offer_id,
                    "event_key": conversion.event_key,
                    "event_type": conversion.event_type,
                    "sale_amount": str(conversion.sale_amount)
                    if conversion.sale_amount is not None
                    else None,
                    "click_id": conversion.click_id,
                    "transaction_id": conversion.transaction_id,
                    "status": conversion.status,
                    "payout": str(conversion.payout),
                    "currency": conversion.currency,
                    "version": conversion.version,
                    "tracking": click.attributes or {},
                    "advertiser_parameters": conversion.advertiser_parameters or {},
                },
            },
            recipient_id=link.affiliate_id,
        )
    from app.features.accounts.notifications import notify

    notify(
        db,
        user.workspace_id,
        [link.affiliate_id],
        "conversion.updated",
        "Conversion status updated",
        conversion.status,
        conversion.id,
    )
    audit(db, user, "conversion.status", conversion.id, f"{previous} -> {status}")
