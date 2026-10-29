"""Campaign management extensions derived from the network operating requirements."""

from decimal import Decimal
from html import escape
from typing import Literal
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, HttpUrl, model_validator
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from .security import DB, Actor
from .schemas import StrictModel, ActiveUpdate
from .models import Offer, User, CampaignApplication, TrackingLink
from .network_models import (
    CampaignEvent,
    CommissionRule,
    CampaignAsset,
    CampaignDestination,
    TrafficRule,
    PeriodCap,
    PeriodCapCounter,
    PostbackReceipt,
    CampaignTarget,
)
from .campaign_policy import (
    ProfileBody,
    EventBody,
    Conditions,
    can_discover,
    public_settings,
    safe_url,
)
from .campaigns import manage_campaign, campaign_output, application_output
from .routes import owned, entitled
from .services import audit

router = APIRouter(tags=["Network campaign operations"])


def accessible(db, user, campaign_id):
    entitled(db, user)
    campaign = owned(db, Offer, campaign_id, user)
    if campaign.campaign_model not in {"CPL", "CPS"}:
        raise HTTPException(404, "Campaign not found")
    if not can_discover(db, user, campaign):
        raise HTTPException(404, "Campaign not found")
    return campaign


def bounded_create(db, user, campaign_id, model, values, maximum=100):
    entitled(db, user)
    manage_campaign(db, user, campaign_id, lock=True)
    if (
        db.scalar(
            select(func.count())
            .select_from(model)
            .where(model.workspace_id == user.workspace_id, model.offer_id == campaign_id)
        )
        >= maximum
    ):
        raise HTTPException(409, "Campaign configuration limit reached")
    row = model(workspace_id=user.workspace_id, offer_id=campaign_id, **values)
    db.add(row)
    try:
        db.flush()
        audit(db, user, model.__tablename__ + ".created", row.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This campaign configuration already exists") from None
    return row


def serialize(row, exclude=()):
    return {
        c.name: str(getattr(row, c.name))
        if isinstance(getattr(row, c.name), Decimal)
        else getattr(row, c.name)
        for c in row.__table__.columns
        if c.name not in {"workspace_id", *exclude}
    }


def scoped_rows(db, user, campaign_id, model):
    accessible(db, user, campaign_id)
    return list(
        db.scalars(
            select(model)
            .where(model.workspace_id == user.workspace_id, model.offer_id == campaign_id)
            .order_by(model.id)
            .limit(100)
        )
    )


def active_update(db, user, campaign_id, row_id, model, body):
    manage_campaign(db, user, campaign_id, lock=True)
    row = owned(db, model, row_id, user)
    if row.offer_id != campaign_id:
        raise HTTPException(404, "Configuration not found")
    row.active = body.active
    audit(db, user, model.__tablename__ + ".active", row.id, str(body.active))
    db.commit()
    return serialize(row)


def update_configuration(db, user, campaign_id, row_id, model, body):
    entitled(db, user)
    manage_campaign(db, user, campaign_id, lock=True)
    row = owned(db, model, row_id, user)
    if row.offer_id != campaign_id:
        raise HTTPException(404, "Configuration not found")
    values = body.model_dump()
    if "active" not in body.model_fields_set:
        values.pop("active", None)
    for name, value in values.items():
        setattr(row, name, str(value) if isinstance(value, HttpUrl) else value)
    audit(db, user, model.__tablename__ + ".updated", row.id)
    db.commit()
    return serialize(row)


@router.get("/campaigns/{campaign_id}/profile")
def profile(campaign_id: str, db: DB, user: Actor):
    campaign = accessible(db, user, campaign_id)
    return {**campaign_output(campaign, user), "settings": public_settings(campaign, user)}


@router.put("/campaigns/{campaign_id}/profile")
def update_profile(campaign_id: str, body: ProfileBody, db: DB, user: Actor):
    entitled(db, user)
    campaign = manage_campaign(db, user, campaign_id, lock=True)
    campaign.name = body.name
    campaign.description = body.description
    campaign.destination_url = str(body.destination_url)
    campaign.catalog_settings = body.settings.model_dump(mode="json")
    audit(db, user, "campaign.profile_updated", campaign.id)
    db.commit()
    return {**campaign_output(campaign, user), "settings": public_settings(campaign, user)}


class Assignment(StrictModel):
    publisher_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    status: Literal["approved", "rejected"] = "approved"
    note: str = Field(default="", max_length=1000)


@router.post("/campaigns/{campaign_id}/assign")
def assign(campaign_id: str, body: Assignment, db: DB, user: Actor):
    entitled(db, user)
    manage_campaign(db, user, campaign_id, lock=True)
    publisher = owned(db, User, body.publisher_id, user)
    if publisher.role != "publisher" or not publisher.active:
        raise HTTPException(422, "Select an active publisher in this network")
    row = db.scalar(
        select(CampaignApplication)
        .where(
            CampaignApplication.workspace_id == user.workspace_id,
            CampaignApplication.offer_id == campaign_id,
            CampaignApplication.publisher_id == publisher.id,
        )
        .with_for_update()
    )
    if not row:
        row = CampaignApplication(
            workspace_id=user.workspace_id, offer_id=campaign_id, publisher_id=publisher.id
        )
        db.add(row)
        db.flush()
    row.status = body.status
    row.review_note = body.note
    row.reviewed_by = user.id
    from .network_account import notify

    notify(
        db,
        user.workspace_id,
        [publisher.id],
        "campaign.access",
        "Campaign access updated",
        body.status,
        campaign_id,
    )
    audit(db, user, "campaign.publisher_assigned", row.id, body.status)
    db.commit()
    return application_output(row)


@router.get("/campaigns/{campaign_id}/events")
def events(campaign_id: str, db: DB, user: Actor):
    rows = scoped_rows(db, user, campaign_id, CampaignEvent)
    hidden = ("revenue", "auto_approve") if user.role in {"publisher", "affiliate"} else ()
    return [serialize(row, hidden) for row in rows]


@router.post("/campaigns/{campaign_id}/events", status_code=201)
def create_event(campaign_id: str, body: EventBody, db: DB, user: Actor):
    return serialize(bounded_create(db, user, campaign_id, CampaignEvent, body.model_dump()))


@router.put("/campaigns/{campaign_id}/events/{event_id}")
def edit_event(campaign_id: str, event_id: str, body: EventBody, db: DB, user: Actor):
    entitled(db, user)
    manage_campaign(db, user, campaign_id, lock=True)
    row = owned(db, CampaignEvent, event_id, user)
    if row.offer_id != campaign_id:
        raise HTTPException(404, "Event not found")
    if row.code != body.code:
        raise HTTPException(409, "Event codes are immutable; create a new event instead")
    for k, v in body.model_dump().items():
        setattr(row, k, v)
    audit(db, user, "campaign.event_updated", row.id)
    db.commit()
    return serialize(row)


class RateBody(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    publisher_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    event_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    conditions: Conditions = Field(default_factory=Conditions)
    priority: int = Field(default=100, ge=1, le=10000)
    model: Literal["CPL", "CPS"] = "CPL"
    payout: Decimal = Field(default=0, ge=0, max_digits=14, decimal_places=4)
    revenue: Decimal = Field(default=0, ge=0, max_digits=14, decimal_places=4)
    sale_percentage: Decimal | None = Field(
        default=None, gt=0, le=100, max_digits=7, decimal_places=4
    )
    visible_to_publishers: bool = True
    active: bool = True

    @model_validator(mode="after")
    def valid(self):
        if (self.model == "CPS") != (self.sale_percentage is not None):
            raise ValueError("CPS requires a percentage; CPL does not accept one")
        return self


def validate_event_reference(db, user, campaign_id, code):
    if code not in {None, "initial"} and not db.scalar(
        select(CampaignEvent.id).where(
            CampaignEvent.workspace_id == user.workspace_id,
            CampaignEvent.offer_id == campaign_id,
            CampaignEvent.code == code,
        )
    ):
        raise HTTPException(422, "Unknown campaign event")


@router.post("/campaigns/{campaign_id}/commission-rules", status_code=201)
def create_rate(campaign_id: str, body: RateBody, db: DB, user: Actor):
    manage_campaign(db, user, campaign_id)
    if body.publisher_id:
        publisher = owned(db, User, body.publisher_id, user)
        if publisher.role != "publisher" or not publisher.active:
            raise HTTPException(422, "Select an active publisher")
    validate_event_reference(db, user, campaign_id, body.event_key)
    return serialize(
        bounded_create(db, user, campaign_id, CommissionRule, body.model_dump(mode="json"))
    )


@router.put("/campaigns/{campaign_id}/commission-rules/{rule_id}")
def edit_rate(campaign_id: str, rule_id: str, body: RateBody, db: DB, user: Actor):
    manage_campaign(db, user, campaign_id)
    if body.publisher_id:
        publisher = owned(db, User, body.publisher_id, user)
        if publisher.role != "publisher" or not publisher.active:
            raise HTTPException(422, "Select an active publisher")
    validate_event_reference(db, user, campaign_id, body.event_key)
    return update_configuration(db, user, campaign_id, rule_id, CommissionRule, body)


@router.get("/campaigns/{campaign_id}/commission-rules")
def rates(campaign_id: str, db: DB, user: Actor):
    rows = scoped_rows(db, user, campaign_id, CommissionRule)
    if user.role in {"publisher", "affiliate"}:
        rows = [
            r
            for r in rows
            if r.active and r.visible_to_publishers and r.publisher_id in {None, user.id}
        ]
    return [
        serialize(r, ("revenue",) if user.role in {"publisher", "affiliate"} else ()) for r in rows
    ]


@router.patch("/campaigns/{campaign_id}/commission-rules/{rule_id}")
def rate_active(campaign_id: str, rule_id: str, body: ActiveUpdate, db: DB, user: Actor):
    return active_update(db, user, campaign_id, rule_id, CommissionRule, body)


class AssetBody(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    kind: Literal["banner", "logo", "text"] = "banner"
    url: HttpUrl
    description: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def valid(self):
        safe_url(self.url)
        return self


@router.post("/campaigns/{campaign_id}/creatives", status_code=201)
def create_asset(campaign_id: str, body: AssetBody, db: DB, user: Actor):
    return serialize(
        bounded_create(db, user, campaign_id, CampaignAsset, body.model_dump(mode="json"))
    )


@router.put("/campaigns/{campaign_id}/creatives/{asset_id}")
def edit_asset(campaign_id: str, asset_id: str, body: AssetBody, db: DB, user: Actor):
    return update_configuration(db, user, campaign_id, asset_id, CampaignAsset, body)


@router.get("/campaigns/{campaign_id}/creatives")
def assets(campaign_id: str, db: DB, user: Actor):
    return [
        serialize(r)
        for r in scoped_rows(db, user, campaign_id, CampaignAsset)
        if r.active or user.role in {"admin", "staff", "advertiser"}
    ]


@router.patch("/campaigns/{campaign_id}/creatives/{asset_id}")
def asset_active(campaign_id: str, asset_id: str, body: ActiveUpdate, db: DB, user: Actor):
    return active_update(db, user, campaign_id, asset_id, CampaignAsset, body)


@router.get("/campaigns/{campaign_id}/creatives/{asset_id}/snippet")
def creative_snippet(campaign_id: str, asset_id: str, db: DB, user: Actor):
    accessible(db, user, campaign_id)
    asset = owned(db, CampaignAsset, asset_id, user)
    if not asset.active or asset.offer_id != campaign_id:
        raise HTTPException(404, "Creative not found")
    link = db.scalar(
        select(TrackingLink).where(
            TrackingLink.workspace_id == user.workspace_id,
            TrackingLink.offer_id == campaign_id,
            TrackingLink.affiliate_id == user.id,
        )
    )
    if not link or user.role != "publisher":
        raise HTTPException(403, "Generate your approved publisher link first")
    from .tracking_runtime import resolve_link

    resolve_link(db, link.id)
    from .domains import link_base

    base = link_base(db, user, None)
    from .routes import link_url

    tracking_url = link_url(db, base, link)
    db.commit()
    target = escape(tracking_url, quote=True)
    content = (
        escape(asset.name)
        if asset.kind == "text"
        else f'<img src="{escape(asset.url, quote=True)}" alt="{escape(asset.name, quote=True)}">'
    )
    return {
        "html": f'<a href="{target}" rel="sponsored noopener">{content}</a>',
        "tracking_url": tracking_url,
        "impression_url": base + "/i/" + link.id,
    }


class DestinationBody(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    url: HttpUrl
    weight: int = Field(default=1, ge=1, le=10000)
    countries: list[str] = Field(default_factory=list, max_length=249)
    publisher_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid(self):
        import pycountry
        import re

        safe_url(self.url, templates=True)
        if any(pycountry.countries.get(alpha_2=c) is None for c in self.countries):
            raise ValueError("Use valid country codes")
        if any(not re.fullmatch(r"[a-f0-9]{32}", p) for p in self.publisher_ids):
            raise ValueError("Invalid publisher ID")
        return self


@router.post("/campaigns/{campaign_id}/destinations", status_code=201)
def create_destination(campaign_id: str, body: DestinationBody, db: DB, user: Actor):
    manage_campaign(db, user, campaign_id)
    for publisher_id in body.publisher_ids:
        if owned(db, User, publisher_id, user).role != "publisher":
            raise HTTPException(422, "Select publishers in this network")
    return serialize(
        bounded_create(db, user, campaign_id, CampaignDestination, body.model_dump(mode="json"))
    )


@router.put("/campaigns/{campaign_id}/destinations/{destination_id}")
def edit_destination(
    campaign_id: str, destination_id: str, body: DestinationBody, db: DB, user: Actor
):
    manage_campaign(db, user, campaign_id)
    for publisher_id in body.publisher_ids:
        if owned(db, User, publisher_id, user).role != "publisher":
            raise HTTPException(422, "Select publishers in this network")
    return update_configuration(db, user, campaign_id, destination_id, CampaignDestination, body)


@router.get("/campaigns/{campaign_id}/destinations")
def destinations(campaign_id: str, db: DB, user: Actor):
    rows = scoped_rows(db, user, campaign_id, CampaignDestination)
    if user.role in {"publisher", "affiliate"}:
        rows = [r for r in rows if r.active and (not r.publisher_ids or user.id in r.publisher_ids)]
    return [
        serialize(r, ("publisher_ids",) if user.role in {"publisher", "affiliate"} else ())
        for r in rows
    ]


@router.patch("/campaigns/{campaign_id}/destinations/{destination_id}")
def destination_active(
    campaign_id: str, destination_id: str, body: ActiveUpdate, db: DB, user: Actor
):
    return active_update(db, user, campaign_id, destination_id, CampaignDestination, body)


class TrafficRuleBody(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    conditions: Conditions
    priority: int = Field(default=100, ge=1, le=10000)
    phase: Literal["click", "conversion"] = "click"
    action: Literal["allow", "reject", "pending", "approve"] = "reject"

    @model_validator(mode="after")
    def valid(self):
        if self.phase == "click" and self.action not in {"allow", "reject"}:
            raise ValueError("Click rules can allow or reject traffic")
        if self.phase == "conversion" and self.action == "allow":
            raise ValueError("Choose pending, approve or reject for a conversion")
        return self


@router.post("/campaigns/{campaign_id}/traffic-rules", status_code=201)
def create_traffic_rule(campaign_id: str, body: TrafficRuleBody, db: DB, user: Actor):
    return serialize(
        bounded_create(db, user, campaign_id, TrafficRule, body.model_dump(mode="json"))
    )


@router.put("/campaigns/{campaign_id}/traffic-rules/{rule_id}")
def edit_traffic_rule(campaign_id: str, rule_id: str, body: TrafficRuleBody, db: DB, user: Actor):
    return update_configuration(db, user, campaign_id, rule_id, TrafficRule, body)


@router.get("/campaigns/{campaign_id}/traffic-rules")
def traffic_rules(campaign_id: str, db: DB, user: Actor):
    manage_campaign(db, user, campaign_id)
    return [serialize(r) for r in scoped_rows(db, user, campaign_id, TrafficRule)]


@router.patch("/campaigns/{campaign_id}/traffic-rules/{rule_id}")
def traffic_rule_active(campaign_id: str, rule_id: str, body: ActiveUpdate, db: DB, user: Actor):
    return active_update(db, user, campaign_id, rule_id, TrafficRule, body)


class CapBody(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    publisher_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    event_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    metric: Literal["clicks", "conversions", "revenue", "payout"] = "conversions"
    period: Literal["hour", "day", "week", "month", "total"] = "day"
    timezone: str = Field(default="UTC", max_length=64)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=4)

    @model_validator(mode="after")
    def valid(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use a supported IANA timezone") from None
        if self.metric in {"clicks", "conversions"} and self.amount != int(self.amount):
            raise ValueError("Count caps require whole numbers")
        if self.metric == "clicks" and self.event_key:
            raise ValueError("Click caps do not have a conversion event")
        return self


@router.post("/campaigns/{campaign_id}/caps", status_code=201)
def create_cap(campaign_id: str, body: CapBody, db: DB, user: Actor):
    manage_campaign(db, user, campaign_id)
    validate_event_reference(db, user, campaign_id, body.event_key)
    if body.publisher_id and owned(db, User, body.publisher_id, user).role != "publisher":
        raise HTTPException(422, "Select a publisher")
    return serialize(bounded_create(db, user, campaign_id, PeriodCap, body.model_dump(mode="json")))


@router.put("/campaigns/{campaign_id}/caps/{cap_id}")
def edit_cap(campaign_id: str, cap_id: str, body: CapBody, db: DB, user: Actor):
    manage_campaign(db, user, campaign_id)
    validate_event_reference(db, user, campaign_id, body.event_key)
    if body.publisher_id and owned(db, User, body.publisher_id, user).role != "publisher":
        raise HTTPException(422, "Select a publisher")
    # Existing counters describe a particular metric, scope and period. Reusing
    # them after changing those dimensions would enforce the wrong limit.
    row = owned(db, PeriodCap, cap_id, user)
    if row.offer_id != campaign_id:
        raise HTTPException(404, "Configuration not found")
    if any(
        getattr(row, key) != getattr(body, key)
        for key in ("publisher_id", "event_key", "metric", "period", "timezone")
    ):
        raise HTTPException(
            409, "Create a new cap to change its metric, period, timezone or publisher/event scope"
        )
    return update_configuration(db, user, campaign_id, cap_id, PeriodCap, body)


@router.get("/campaigns/{campaign_id}/caps")
def caps(campaign_id: str, db: DB, user: Actor):
    from .period_caps import bucket, historical_usage
    from .campaign_policy import policy

    campaign = manage_campaign(db, user, campaign_id)
    rows = scoped_rows(db, user, campaign_id, PeriodCap)
    result = []
    for row in rows:
        key, start, end = bucket(row, datetime.now(timezone.utc))
        counter = db.scalar(
            select(PeriodCapCounter.amount).where(
                PeriodCapCounter.workspace_id == user.workspace_id,
                PeriodCapCounter.cap_id == row.id,
                PeriodCapCounter.bucket == key,
            )
        )
        result.append(
            {
                **serialize(row),
                "current_bucket": key,
                "alert_percent": policy(campaign).cap_alert_percent,
                "recorded_usage": str(
                    counter if counter is not None else historical_usage(db, row, start, end)
                ),
            }
        )
    return result


@router.patch("/campaigns/{campaign_id}/caps/{cap_id}")
def cap_active(campaign_id: str, cap_id: str, body: ActiveUpdate, db: DB, user: Actor):
    return active_update(db, user, campaign_id, cap_id, PeriodCap, body)


class TargetBody(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    partner_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    event_key: str = Field(default="initial", pattern=r"^[a-z][a-z0-9_]{0,63}$")
    amount: int = Field(gt=0, le=100_000_000)
    starts_on: date
    ends_on: date

    @model_validator(mode="after")
    def valid(self):
        if self.ends_on < self.starts_on:
            raise ValueError("The end date must be on or after the start date")
        return self


def check_target(db, user, campaign_id, body):
    manage_campaign(db, user, campaign_id)
    validate_event_reference(db, user, campaign_id, body.event_key)
    if body.partner_id and owned(db, User, body.partner_id, user).role not in {
        "affiliate",
        "publisher",
    }:
        raise HTTPException(422, "Select an agency or partner")


def target_progress(db, target, today=None):
    """Counts non-rejected conversions in the target's UTC date range; an agency includes its publishers."""
    from datetime import timedelta
    from .models import Click, Conversion

    query = (
        select(func.count(), func.coalesce(func.sum(Conversion.payout), 0))
        .select_from(Conversion)
        .join(Click, Click.id == Conversion.click_id)
        .join(TrackingLink, TrackingLink.id == Click.link_id)
        .where(
            Conversion.workspace_id == target.workspace_id,
            Conversion.offer_id == target.offer_id,
            Conversion.event_key == target.event_key,
            Conversion.status != "rejected",
            Conversion.created_at
            >= datetime.combine(target.starts_on, datetime.min.time(), timezone.utc),
            Conversion.created_at
            < datetime.combine(
                target.ends_on + timedelta(days=1), datetime.min.time(), timezone.utc
            ),
        )
    )
    if target.partner_id:
        team = select(User.id).where(
            User.workspace_id == target.workspace_id,
            (User.id == target.partner_id) | (User.parent_user_id == target.partner_id),
        )
        query = query.where(TrackingLink.affiliate_id.in_(team))
    achieved, commission = db.execute(query).one()
    today = today or datetime.now(timezone.utc).date()
    days = (target.ends_on - target.starts_on).days + 1
    elapsed = min(max((today - target.starts_on).days + 1, 0), days)
    expected = target.amount * elapsed // days
    pace = (
        "not_started"
        if today < target.starts_on
        else ("met" if achieved >= target.amount else "missed")
        if today > target.ends_on
        else "ahead"
        if achieved >= expected
        else "behind"
    )
    return {
        **serialize(target),
        "achieved": achieved,
        "percent": achieved * 100 // target.amount,
        "expected_by_today": expected,
        "pace": pace,
        "commission": str(commission),
    }


@router.post("/campaigns/{campaign_id}/targets", status_code=201)
def create_target(campaign_id: str, body: TargetBody, db: DB, user: Actor):
    check_target(db, user, campaign_id, body)
    row = bounded_create(db, user, campaign_id, CampaignTarget, body.model_dump())
    return target_progress(db, row)


@router.put("/campaigns/{campaign_id}/targets/{target_id}")
def edit_target(campaign_id: str, target_id: str, body: TargetBody, db: DB, user: Actor):
    check_target(db, user, campaign_id, body)
    update_configuration(db, user, campaign_id, target_id, CampaignTarget, body)
    return target_progress(db, owned(db, CampaignTarget, target_id, user))


@router.get("/campaigns/{campaign_id}/targets")
def targets(campaign_id: str, db: DB, user: Actor):
    manage_campaign(db, user, campaign_id)
    return [target_progress(db, row) for row in scoped_rows(db, user, campaign_id, CampaignTarget)]


@router.patch("/campaigns/{campaign_id}/targets/{target_id}")
def target_active(campaign_id: str, target_id: str, body: ActiveUpdate, db: DB, user: Actor):
    return active_update(db, user, campaign_id, target_id, CampaignTarget, body)


@router.get("/targets")
def all_targets(db: DB, user: Actor):
    """Active targets across campaigns, for the brand home dashboard."""
    if user.role not in {"admin", "staff", "advertiser"}:
        raise HTTPException(403, "Campaign management role required")
    query = (
        select(CampaignTarget, Offer.name, Offer.currency)
        .join(Offer, Offer.id == CampaignTarget.offer_id)
        .where(CampaignTarget.workspace_id == user.workspace_id, CampaignTarget.active.is_(True))
        .order_by(CampaignTarget.ends_on, CampaignTarget.id)
        .limit(100)
    )
    if user.role == "advertiser":
        query = query.where(Offer.advertiser_id == user.id)
    return [
        {**target_progress(db, row), "campaign_name": name, "currency": currency}
        for row, name, currency in db.execute(query)
    ]


@router.get("/postback-receipts")
def postback_receipts(
    db: DB,
    user: Actor,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    offer_id: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
):
    query = (
        select(PostbackReceipt)
        .join(Offer, Offer.id == PostbackReceipt.offer_id)
        .where(PostbackReceipt.workspace_id == user.workspace_id)
    )
    if user.role == "advertiser":
        query = query.where(Offer.advertiser_id == user.id)
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Advertiser or staff access required")
    if offer_id:
        manage_campaign(db, user, offer_id)
        query = query.where(PostbackReceipt.offer_id == offer_id)
    return [
        serialize(r)
        for r in db.scalars(
            query.order_by(PostbackReceipt.created_at.desc(), PostbackReceipt.id)
            .offset(offset)
            .limit(limit)
        )
    ]
