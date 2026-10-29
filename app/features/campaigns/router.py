"""Network-scoped advertiser campaigns, applications and publisher S2S settings."""

from copy import deepcopy

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.security import DB, Actor
from app.core.services import audit
from app.features.campaigns.offers import rotate_key
from app.features.campaigns.schemas import (
    ApplicationBody,
    CampaignBody,
    CloneBody,
    PublisherPostbackBody,
    ReviewBody,
)
from app.features.campaigns.service import (
    application_output,
    campaign_output,
    create_campaign_record,
)
from app.features.domains.router import link_base
from app.features.tracking.service import link_url
from app.permissions.campaigns import manage_campaign
from app.permissions.resources import entitled, owned
from app.persistence.models import (
    CampaignApplication,
    Offer,
    TrackingLink,
    User,
    WebhookEndpoint,
)
from app.shared.schemas import ActiveUpdate

router = APIRouter(tags=["Campaigns and applications"])


@router.post("/campaigns", status_code=201)
def new_campaign(body: CampaignBody, db: DB, user: Actor):
    campaign, key = create_campaign_record(body, db, user)
    db.commit()
    return {**campaign_output(campaign, user), "postback_key": key}


@router.get("/campaigns")
def catalog(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    entitled(db, user)
    query = select(Offer).where(
        Offer.workspace_id == user.workspace_id, Offer.campaign_model.in_(["CPL", "CPS"])
    )
    if user.role == "advertiser":
        query = query.where(Offer.advertiser_id == user.id)
    elif user.role in {"publisher", "affiliate"}:
        private_access = (
            select(CampaignApplication.id)
            .where(
                CampaignApplication.workspace_id == user.workspace_id,
                CampaignApplication.offer_id == Offer.id,
                CampaignApplication.publisher_id == user.id,
                CampaignApplication.status == "approved",
            )
            .exists()
        )
        query = query.where(
            Offer.active.is_(True),
            or_(
                Offer.catalog_settings["visibility"].as_string().is_(None),
                Offer.catalog_settings["visibility"].as_string() != "private",
                private_access,
            ),
        )
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Campaign access required")
    return [
        campaign_output(c, user)
        for c in db.scalars(query.order_by(Offer.id).offset(offset).limit(limit))
    ]


@router.post("/campaigns/{campaign_id}/clone", status_code=201)
def clone_campaign(campaign_id: str, body: CloneBody, db: DB, user: Actor):
    """Copy campaign configuration into a paused campaign with fresh credentials."""
    from app.persistence.network_models import (
        CampaignAsset,
        CampaignDestination,
        CampaignEvent,
        CommissionRule,
        PeriodCap,
        TrafficRule,
    )

    entitled(db, user)
    source = manage_campaign(db, user, campaign_id, lock=True)
    copied, key = create_campaign_record(
        CampaignBody(
            name=body.name,
            destination_url=source.destination_url,
            revenue=source.revenue,
            payout=source.payout,
            currency=source.currency,
            advertiser_id=source.advertiser_id,
            attribution_hours=source.attribution_hours,
            campaign_model=source.campaign_model,
            sale_percentage=source.sale_percentage,
            description=source.description,
        ),
        db,
        user,
    )
    copied.active = False
    copied.tracking_rules = deepcopy(source.tracking_rules)
    copied.catalog_settings = deepcopy(source.catalog_settings)
    # External identifiers must remain distinct from the original campaign.
    copied.catalog_settings.pop("external_id", None)
    # Only configuration tables are allowlisted. Never copy approvals, credentials,
    # links, conversion records, financial history or accumulated cap counters.
    for model in (
        CampaignEvent,
        CommissionRule,
        CampaignAsset,
        CampaignDestination,
        TrafficRule,
        PeriodCap,
    ):
        for row in db.scalars(
            select(model).where(
                model.workspace_id == user.workspace_id,
                model.offer_id == source.id,
            )
        ):
            values = {
                column.key: deepcopy(getattr(row, column.key))
                for column in model.__table__.columns
                if column.key not in {"id", "workspace_id", "offer_id"}
            }
            db.add(model(workspace_id=user.workspace_id, offer_id=copied.id, **values))
    audit(db, user, "campaign.cloned", copied.id, source.id)
    db.commit()
    return {**campaign_output(copied, user), "postback_key": key}


@router.patch("/campaigns/{campaign_id}")
def campaign_status(campaign_id: str, body: ActiveUpdate, db: DB, user: Actor):
    entitled(db, user)
    campaign = manage_campaign(db, user, campaign_id, lock=True)
    campaign.active = body.active
    audit(db, user, "campaign.active", campaign.id, str(body.active))
    db.commit()
    return campaign_output(campaign, user)


@router.post("/campaigns/{campaign_id}/postback-key")
def campaign_key(campaign_id: str, db: DB, user: Actor):
    manage_campaign(db, user, campaign_id)
    return rotate_key(campaign_id, db, user)


@router.post("/campaigns/{campaign_id}/apply", status_code=201)
def apply(campaign_id: str, body: ApplicationBody, db: DB, user: Actor):
    entitled(db, user)
    if user.role != "publisher":
        raise HTTPException(403, "Publisher access required")
    campaign = owned(db, Offer, campaign_id, user)
    if (
        not campaign.active
        or campaign.campaign_model not in {"CPL", "CPS"}
        or not campaign.advertiser_id
    ):
        raise HTTPException(409, "Campaign is not accepting applications")
    existing = db.scalar(
        select(CampaignApplication).where(
            CampaignApplication.workspace_id == user.workspace_id,
            CampaignApplication.offer_id == campaign_id,
            CampaignApplication.publisher_id == user.id,
        )
    )
    if existing:
        return application_output(existing)
    from app.features.campaigns.policy import policy

    visibility = policy(campaign).visibility
    if visibility == "private":
        raise HTTPException(404, "Campaign not found")
    application = CampaignApplication(
        workspace_id=user.workspace_id,
        offer_id=campaign_id,
        publisher_id=user.id,
        message=body.message,
        status="approved" if visibility == "open" else "pending",
    )
    db.add(application)
    try:
        db.flush()
        audit(db, user, "campaign.applied", application.id)
        from app.features.accounts.notifications import notify, notify_admins

        notify(
            db,
            user.workspace_id,
            [campaign.advertiser_id],
            "campaign.application",
            "Publisher campaign application",
            campaign.name,
            application.id,
        )
        notify_admins(
            db,
            user.workspace_id,
            "campaign.application",
            "Publisher campaign application",
            campaign.name,
            application.id,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Application already submitted") from None
    return application_output(application)


@router.get("/campaign-applications")
def applications(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    entitled(db, user)
    query = (
        select(CampaignApplication, Offer.name, User.email)
        .join(Offer, Offer.id == CampaignApplication.offer_id)
        .join(User, User.id == CampaignApplication.publisher_id)
        .where(CampaignApplication.workspace_id == user.workspace_id)
    )
    if user.role == "publisher":
        query = query.where(CampaignApplication.publisher_id == user.id)
    elif user.role == "advertiser":
        query = query.where(Offer.advertiser_id == user.id)
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Application access required")
    return [
        {**application_output(a), "campaign_name": name, "publisher_email": email}
        for a, name, email in db.execute(
            query.order_by(CampaignApplication.created_at.desc(), CampaignApplication.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@router.patch("/campaign-applications/{application_id}")
def review(application_id: str, body: ReviewBody, db: DB, user: Actor):
    entitled(db, user)
    row = db.scalar(
        select(CampaignApplication)
        .where(
            CampaignApplication.id == application_id,
            CampaignApplication.workspace_id == user.workspace_id,
        )
        .with_for_update()
    )
    if not row:
        raise HTTPException(404, "Application not found")
    manage_campaign(db, user, row.offer_id)
    row.status, row.review_note, row.reviewed_by = body.status, body.note, user.id
    audit(db, user, "campaign.application_reviewed", row.id, body.status)
    from app.features.accounts.notifications import notify

    notify(
        db,
        user.workspace_id,
        [row.publisher_id],
        "campaign.access",
        "Campaign application reviewed",
        body.status,
        row.offer_id,
    )
    db.commit()
    return application_output(row)


@router.post("/campaigns/{campaign_id}/link", status_code=201)
def publisher_link(
    campaign_id: str,
    db: DB,
    user: Actor,
    domain_id: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
):
    entitled(db, user)
    if user.role != "publisher":
        raise HTTPException(403, "Publisher access required")
    campaign = owned(db, Offer, campaign_id, user)
    application = db.scalar(
        select(CampaignApplication)
        .where(
            CampaignApplication.offer_id == campaign.id,
            CampaignApplication.publisher_id == user.id,
            CampaignApplication.workspace_id == user.workspace_id,
        )
        .with_for_update()
    )
    if not application or application.status != "approved" or not campaign.active:
        raise HTTPException(403, "An approved application and active campaign are required")
    base = link_base(db, user, domain_id, advertiser_id=campaign.advertiser_id)
    link = db.scalar(
        select(TrackingLink).where(
            TrackingLink.workspace_id == user.workspace_id,
            TrackingLink.offer_id == campaign.id,
            TrackingLink.affiliate_id == user.id,
        )
    )
    if not link:
        link = TrackingLink(
            workspace_id=user.workspace_id, offer_id=campaign.id, affiliate_id=user.id
        )
        db.add(link)
        db.flush()
        audit(db, user, "campaign.link_created", link.id)
    db.commit()
    from app.features.domains.policy import enabled
    from app.persistence.models import LandingPage, TrackingDomain

    pages = db.execute(
        select(LandingPage, TrackingDomain)
        .join(TrackingDomain, TrackingDomain.id == LandingPage.domain_id)
        .where(
            LandingPage.workspace_id == user.workspace_id,
            LandingPage.offer_id == campaign.id,
            LandingPage.active.is_(True),
            TrackingDomain.advertiser_id == campaign.advertiser_id,
            TrackingDomain.purpose.in_(["landing", "both"]),
        )
        .order_by(LandingPage.id)
        .limit(100)
    )
    url = link_url(db, base, link)
    db.commit()  # keep the public numbers assigned for this link
    return {
        "id": link.id,
        "url": url,
        "capture_url": f"{base}/capture/{link.id}",
        "impression_url": f"{base}/i/{link.id}",
        "sdk_url": get_settings().public_base_url.rstrip("/") + "/sdk/relay.js",
        "final_urls": [
            f"https://{domain.hostname}/l/{page.slug}?link={link.id}"
            for page, domain in pages
            if enabled(domain)
        ],
    }


@router.get("/publisher-postback")
def publisher_postback(db: DB, user: Actor):
    if user.role != "publisher":
        raise HTTPException(403, "Publisher access required")
    row = db.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.workspace_id == user.workspace_id,
            WebhookEndpoint.affiliate_id == user.id,
            WebhookEndpoint.offer_id.is_(None),
        )
    )
    from app.features.callbacks.templates import stored_url

    return {"url": stored_url(row) if row else None, "active": row.active if row else False}


@router.put("/publisher-postback")
def set_publisher_postback(body: PublisherPostbackBody, db: DB, user: Actor):
    if user.role != "publisher":
        raise HTTPException(403, "Publisher access required")
    from app.features.portals.router import WebhookBody, configure_webhook

    return configure_webhook(WebhookBody(affiliate_id=user.id, url=body.url), db, user)


@router.get("/campaigns/{campaign_id}/tracking-domains")
def campaign_domains(campaign_id: str, db: DB, user: Actor):
    from app.features.domains.policy import enabled
    from app.persistence.models import TrackingDomain

    entitled(db, user)
    campaign = owned(db, Offer, campaign_id, user)
    from app.features.campaigns.policy import can_discover

    if not can_discover(db, user, campaign):
        raise HTTPException(404, "Campaign not found")
    if user.role == "advertiser" and campaign.advertiser_id != user.id:
        raise HTTPException(404, "Campaign not found")
    if user.role not in {"admin", "staff", "advertiser", "publisher"}:
        raise HTTPException(403, "Campaign access required")
    return [
        {"id": d.id, "hostname": d.hostname}
        for d in db.scalars(
            select(TrackingDomain)
            .where(
                TrackingDomain.workspace_id == user.workspace_id,
                TrackingDomain.advertiser_id == campaign.advertiser_id,
                TrackingDomain.purpose.in_(["tracking", "both"]),
            )
            .order_by(TrackingDomain.hostname)
            .limit(100)
        )
        if enabled(d)
    ]
