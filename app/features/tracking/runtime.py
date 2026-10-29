"""Shared click authorization for redirects and consent-gated direct-page capture."""

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select

from app.core.services import entitlement
from app.features.campaigns.policy import (
    PASSTHROUGH_PARAMETERS,
    destination,
    matches,
    parameters,
    rule_values,
)
from app.persistence.models import (
    CampaignApplication,
    Click,
    Offer,
    TrackingLink,
    User,
    Workspace,
    identifier,
)


def resolve_link(db, link_id):
    link = db.get(TrackingLink, link_id)
    if not link:
        raise HTTPException(404, "Tracking link not found")
    offer = db.get(Offer, link.offer_id)
    publisher = db.get(User, link.affiliate_id)
    entitlement(db.get(Workspace, link.workspace_id))
    if offer.advertiser_id and not db.get(User, offer.advertiser_id).active:
        raise HTTPException(410, "Advertiser disabled")
    if publisher.role not in {"affiliate", "publisher"}:
        raise HTTPException(410, "Account no longer has publisher access")
    if not offer.active or not publisher.active:
        raise HTTPException(410, "Tracking link disabled")
    if publisher.parent_user_id and not db.get(User, publisher.parent_user_id).active:
        raise HTTPException(410, "Parent affiliate disabled")
    if offer.campaign_model in {"CPL", "CPS"} and publisher.role == "publisher":
        application = db.scalar(
            select(CampaignApplication).where(
                CampaignApplication.workspace_id == link.workspace_id,
                CampaignApplication.offer_id == offer.id,
                CampaignApplication.publisher_id == publisher.id,
            )
        )
        if not application or application.status != "approved":
            raise HTTPException(403, "Publisher is not approved for this campaign")
    return link, offer, publisher


def record_click(db, request, link_id, sub_id=None):
    from app.features.campaigns.caps import reserve as reserve_period_caps
    from app.features.domains.router import enforce_tracking_host
    from app.features.sources.router import blocked_preset, referrer_domain
    from app.features.tracking.rules import Rules, check_click, reject_fraud, reserve
    from app.persistence.network_models import TrafficRule

    link, offer, publisher = resolve_link(db, link_id)
    enforce_tracking_host(db, request, link.workspace_id, offer.advertiser_id)
    metadata = check_click(db, offer, request)
    if sub_id is not None and (len(sub_id) > 100 or any(ord(c) < 32 for c in sub_id)):
        raise HTTPException(422, "Invalid placement sub ID")
    at = datetime.now(timezone.utc)
    rules = Rules.model_validate(offer.tracking_rules)
    if rules.unique_click_hours and metadata.get("ip_hash"):
        earlier = db.scalar(
            select(Click)
            .where(
                Click.workspace_id == link.workspace_id,
                Click.link_id == link.id,
                Click.ip_hash == metadata["ip_hash"],
                Click.created_at >= at - timedelta(hours=rules.unique_click_hours),
            )
            .order_by(Click.created_at.desc())
            .limit(1)
        )
        if earlier:
            target = destination(
                db,
                offer,
                link,
                earlier,
                request.query_params.get("url"),
                request.query_params.get("destination_id"),
                parameters(request.query_params, PASSTHROUGH_PARAMETERS),
            )
            return link, offer, earlier, target
    click = Click(
        id=identifier(),
        workspace_id=link.workspace_id,
        link_id=link.id,
        sub_id=sub_id,
        created_at=at,
        attributes={
            **parameters(request.query_params),
            "referrer": referrer_domain(request),
        },
        **metadata,
    )
    blocked = blocked_preset(db, offer, click.attributes)
    if blocked:
        reject_fraud(
            db, offer, "blocked_source:" + blocked, 100, click.ip_hash, publisher_id=publisher.id
        )
    values = rule_values(click, publisher.id)
    for rule in db.scalars(
        select(TrafficRule)
        .where(
            TrafficRule.workspace_id == offer.workspace_id,
            TrafficRule.offer_id == offer.id,
            TrafficRule.phase == "click",
            TrafficRule.active.is_(True),
        )
        .order_by(TrafficRule.priority, TrafficRule.id)
        .limit(100)
    ):
        if matches(rule.conditions, values):
            if rule.action == "reject":
                raise HTTPException(403, "Traffic rejected by campaign rule")
            break
    target = destination(
        db,
        offer,
        link,
        click,
        request.query_params.get("url"),
        request.query_params.get("destination_id"),
        parameters(request.query_params, PASSTHROUGH_PARAMETERS),
    )
    reserve(db, offer, "clicks", at)
    reserve_period_caps(db, offer, publisher.id, "click", at)
    db.add(click)
    return link, offer, click, target
