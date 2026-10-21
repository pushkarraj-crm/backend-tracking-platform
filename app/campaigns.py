"""Network-scoped advertiser campaigns, applications and publisher S2S settings."""

import secrets
from copy import deepcopy
from typing import Literal
from decimal import Decimal
from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, HttpUrl, model_validator
from sqlalchemy import select, or_
from sqlalchemy.exc import IntegrityError
from .campaign_policy import CatalogSettings, ProfileBody
from .tracking import Rules
from .schemas import StrictModel, OfferCreate, ActiveUpdate
from .security import DB, Actor, key_hash
from .models import Offer, User, CampaignApplication, TrackingLink, WebhookEndpoint
from .routes import owned, entitled, output_offer, quota, rotate_key
from .services import audit
from .domains import link_base
from .config import get_settings

router = APIRouter(tags=["Campaigns and applications"])


class CampaignBody(OfferCreate):
    settings: CatalogSettings = Field(default_factory=CatalogSettings)
    tracking_rules: Rules = Field(default_factory=Rules)
    active: bool = True
    campaign_model: Literal["CPL", "CPS"] = "CPL"
    sale_percentage: Decimal | None = Field(
        default=None, gt=0, le=100, max_digits=7, decimal_places=4
    )
    description: str = Field(default="", max_length=10000)

    @model_validator(mode="after")
    def rate(self):
        from .campaign_policy import safe_url

        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Campaign name cannot be blank")
        safe_url(self.destination_url, templates=True)
        ProfileBody(
            name=self.name,
            description=self.description,
            destination_url=self.destination_url,
            settings=self.settings,
        )
        if self.destination_url.scheme != "https":
            raise ValueError("Campaign destinations must use HTTPS")
        if self.campaign_model == "CPS" and self.sale_percentage is None:
            raise ValueError("CPS campaigns require a sale commission percentage")
        if self.campaign_model == "CPL" and self.sale_percentage is not None:
            raise ValueError("Only CPS campaigns use sale commission percentages")
        return self


class ApplicationBody(StrictModel):
    message: str = Field(default="", max_length=1000)


class CloneBody(StrictModel):
    name: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_name(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Campaign name cannot be blank")
        return self


class ReviewBody(StrictModel):
    status: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=1000)


def manage_campaign(db, user, campaign_id, lock=False):
    campaign = db.scalar(
        select(Offer)
        .where(Offer.id == campaign_id, Offer.workspace_id == user.workspace_id)
        .with_for_update()
        if lock
        else select(Offer).where(Offer.id == campaign_id, Offer.workspace_id == user.workspace_id)
    )
    if (
        not campaign
        or campaign.campaign_model not in {"CPL", "CPS"}
        or (user.role == "advertiser" and campaign.advertiser_id != user.id)
    ):
        raise HTTPException(404, "Campaign not found")
    if user.role not in {"admin", "staff", "advertiser"}:
        raise HTTPException(403, "Campaign management access required")
    return campaign


def campaign_output(campaign, user):
    result = output_offer(campaign)
    from .campaign_policy import public_settings

    result["settings"] = public_settings(campaign, user)
    result.update(
        campaign_model=campaign.campaign_model,
        sale_percentage=str(campaign.sale_percentage)
        if campaign.sale_percentage is not None
        else None,
        description=campaign.description,
    )
    if user.role in {"publisher", "affiliate"}:
        for name in ("revenue", "destination_url", "postback_url"):
            result.pop(name, None)
    return result


def create_campaign_record(body, db, user):
    if user.role not in {"advertiser", "admin", "staff"}:
        raise HTTPException(403, "Advertiser access required")
    if user.role != "advertiser" and not body.advertiser_id:
        raise HTTPException(422, "Select the advertiser responsible for this campaign")
    advertiser_id = user.id if user.role == "advertiser" else body.advertiser_id
    if user.role == "advertiser" and body.advertiser_id not in {None, user.id}:
        raise HTTPException(403, "Cannot create campaigns for another advertiser")
    advertiser = owned(db, User, advertiser_id, user)
    if advertiser.role != "advertiser" or not advertiser.active:
        raise HTTPException(422, "Select an active advertiser")
    quota(db, user, Offer, "offers")
    key = secrets.token_urlsafe(32)
    campaign = Offer(
        workspace_id=user.workspace_id,
        **body.model_dump(
            exclude={"destination_url", "advertiser_id", "settings", "tracking_rules", "active"}
        ),
        destination_url=str(body.destination_url),
        advertiser_id=advertiser_id,
        postback_key_hash=key_hash(key),
        active=body.active,
        catalog_settings=body.settings.model_dump(mode="json"),
        tracking_rules=body.tracking_rules.model_dump(mode="json"),
    )
    db.add(campaign)
    db.flush()
    audit(db, user, "campaign.created", campaign.id)
    return campaign, key


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
    from .network_models import (
        CampaignEvent,
        CommissionRule,
        CampaignAsset,
        CampaignDestination,
        TrafficRule,
        PeriodCap,
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
    from .campaign_policy import policy

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
        from .network_account import notify, notify_admins

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


def application_output(row):
    return {
        k: getattr(row, k)
        for k in (
            "id",
            "offer_id",
            "publisher_id",
            "status",
            "message",
            "review_note",
            "created_at",
        )
    }


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
    from .network_account import notify

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
    from .models import LandingPage, TrackingDomain
    from .domains import enabled

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
    return {
        "id": link.id,
        "url": f"{base}/t/{link.id}",
        "capture_url": f"{base}/capture/{link.id}",
        "impression_url": f"{base}/i/{link.id}",
        "sdk_url": get_settings().public_base_url.rstrip("/") + "/sdk/relay.js",
        "final_urls": [
            f"https://{domain.hostname}/l/{page.slug}?link={link.id}"
            for page, domain in pages
            if enabled(domain)
        ],
    }


class PublisherPostbackBody(StrictModel):
    url: HttpUrl


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
    from .callbacks import stored_url

    return {"url": stored_url(row) if row else None, "active": row.active if row else False}


@router.put("/publisher-postback")
def set_publisher_postback(body: PublisherPostbackBody, db: DB, user: Actor):
    if user.role != "publisher":
        raise HTTPException(403, "Publisher access required")
    from .portals import configure_webhook, WebhookBody

    return configure_webhook(WebhookBody(affiliate_id=user.id, url=body.url), db, user)


@router.get("/campaigns/{campaign_id}/tracking-domains")
def campaign_domains(campaign_id: str, db: DB, user: Actor):
    from .models import TrackingDomain
    from .domains import enabled

    entitled(db, user)
    campaign = owned(db, Offer, campaign_id, user)
    from .campaign_policy import can_discover

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
