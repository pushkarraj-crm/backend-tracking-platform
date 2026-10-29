import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import Field, model_validator
from sqlalchemy import func, select

from app.core.security import DB, Actor
from app.core.services import audit, entitlement
from app.features.campaigns.policy import (
    TRACKED_PARAMETERS,
)
from app.permissions.resources import owned, visible_users
from app.persistence.models import (
    CampaignApplication,
    Offer,
    OfferCounter,
    TrackingLink,
    User,
    Workspace,
)
from app.persistence.network_models import SmartLink
from app.shared.schemas import ActiveUpdate, StrictModel

router = APIRouter(tags=["Network tools"])


class SmartChild(StrictModel):
    link_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    weight: int = Field(default=1, ge=1, le=10000)


class SmartBody(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    publisher_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    children: list[SmartChild] = Field(min_length=2, max_length=50)

    @model_validator(mode="after")
    def valid(self):
        if len({r.link_id for r in self.children}) != len(self.children):
            raise ValueError("Select unique child links")
        return self


@router.post("/smart-links", status_code=201)
def create_smart(body: SmartBody, db: DB, user: Actor):
    publisher = owned(db, User, body.publisher_id or user.id, user)
    if publisher.role != "publisher" or not publisher.active:
        raise HTTPException(422, "Select an active publisher")
    if (
        user.role not in {"admin", "staff"}
        and publisher.id != user.id
        and not (user.role == "affiliate" and publisher.parent_user_id == user.id)
    ):
        raise HTTPException(403, "Can create smart links only for your own publishers")
    links = list(
        db.execute(
            select(TrackingLink, Offer)
            .join(Offer, Offer.id == TrackingLink.offer_id)
            .where(
                TrackingLink.workspace_id == user.workspace_id,
                TrackingLink.affiliate_id == publisher.id,
                TrackingLink.id.in_([c.link_id for c in body.children]),
            )
        )
    )
    if len(links) != len(body.children) or len({o.advertiser_id for _, o in links}) != 1:
        raise HTTPException(
            422, "Child links must belong to this publisher and a single advertiser"
        )
    approved = set(
        db.scalars(
            select(CampaignApplication.offer_id).where(
                CampaignApplication.workspace_id == user.workspace_id,
                CampaignApplication.publisher_id == publisher.id,
                CampaignApplication.status == "approved",
            )
        )
    )
    if any(o.id not in approved or not o.active for _, o in links):
        raise HTTPException(403, "All child campaigns require active approved publisher access")
    db.scalar(select(User).where(User.id == publisher.id).with_for_update())
    if (
        db.scalar(
            select(func.count())
            .select_from(SmartLink)
            .where(SmartLink.publisher_id == publisher.id)
        )
        >= 100
    ):
        raise HTTPException(409, "Smart link limit reached")
    row = SmartLink(
        workspace_id=user.workspace_id,
        publisher_id=publisher.id,
        name=body.name,
        children=[c.model_dump() for c in body.children],
    )
    db.add(row)
    db.flush()
    audit(db, user, "smart_link.created", row.id)
    db.commit()
    return smart_output(db, user, row)


def smart_output(db, user, row):
    from app.features.domains.router import link_base

    return {
        "id": row.id,
        "name": row.name,
        "publisher_id": row.publisher_id,
        "children": row.children,
        "active": row.active,
        "url": link_base(db, user, None) + "/s/" + row.id,
    }


@router.get("/smart-links")
def smart_links(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    query = select(SmartLink).where(SmartLink.workspace_id == user.workspace_id)
    if user.role in {"affiliate", "publisher"}:
        query = query.where(SmartLink.publisher_id.in_(visible_users(user)))
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Publisher or network staff required")
    return [
        smart_output(db, user, row)
        for row in db.scalars(query.order_by(SmartLink.id).offset(offset).limit(limit))
    ]


@router.patch("/smart-links/{smart_id}")
def smart_status(smart_id: str, body: ActiveUpdate, db: DB, user: Actor):
    row = owned(db, SmartLink, smart_id, user)
    if user.role not in {"admin", "staff"} and row.publisher_id not in list(
        db.scalars(visible_users(user))
    ):
        raise HTTPException(404, "Smart link not found")
    row.active = body.active
    audit(db, user, "smart_link.active", row.id, str(body.active))
    db.commit()
    return smart_output(db, user, row)


@router.get("/s/{smart_id}", include_in_schema=False)
def smart_redirect(smart_id: str, request: Request, db: DB):
    from app.features.domains.router import enforce_tracking_host
    from app.features.tracking.rules import Rules, context

    row = db.get(SmartLink, smart_id)
    if not row or not row.active:
        raise HTTPException(404, "Smart link not found")
    publisher = db.get(User, row.publisher_id)
    entitlement(db.get(Workspace, row.workspace_id))
    if not publisher.active or (
        publisher.parent_user_id and not db.get(User, publisher.parent_user_id).active
    ):
        raise HTTPException(410, "Publisher disabled")
    weights = {c["link_id"]: c["weight"] for c in row.children}
    offers = list(
        db.execute(
            select(TrackingLink, Offer)
            .join(Offer, Offer.id == TrackingLink.offer_id)
            .where(
                TrackingLink.workspace_id == row.workspace_id,
                TrackingLink.affiliate_id == row.publisher_id,
                TrackingLink.id.in_(weights),
                Offer.active.is_(True),
            )
        )
    )
    approved = set(
        db.scalars(
            select(CampaignApplication.offer_id).where(
                CampaignApplication.workspace_id == row.workspace_id,
                CampaignApplication.publisher_id == row.publisher_id,
                CampaignApplication.status == "approved",
            )
        )
    )
    metadata, _, _ = context(request)
    now = datetime.now(timezone.utc)
    eligible = []
    counters = {
        (c.offer_id, c.period): c
        for c in db.scalars(
            select(OfferCounter).where(
                OfferCounter.workspace_id == row.workspace_id,
                OfferCounter.offer_id.in_([o.id for _, o in offers]),
                OfferCounter.period.in_(["total", now.date().isoformat()]),
            )
        )
    }
    for link, offer in offers:
        if offer.id not in approved:
            continue
        owner = db.get(User, offer.advertiser_id) if offer.advertiser_id else None
        if owner and not owner.active:
            continue
        rules = Rules.model_validate(offer.tracking_rules)
        if (
            (rules.starts_at and now < rules.starts_at)
            or (rules.ends_at and now >= rules.ends_at)
            or (rules.utc_hours and now.hour not in rules.utc_hours)
        ):
            continue
        if any(
            values and metadata[key] not in values
            for key, values in [
                ("country", rules.countries),
                ("device", rules.devices),
                ("operating_system", rules.operating_systems),
                ("language", rules.languages),
            ]
        ):
            continue
        limited = False
        for period, limit_value in [
            ("total", rules.total_clicks),
            (now.date().isoformat(), rules.daily_clicks),
        ]:
            counter = counters.get((offer.id, period))
            if limit_value is not None and counter and counter.clicks >= limit_value:
                limited = True
        if not limited:
            eligible.append((link, offer))
    if not eligible:
        raise HTTPException(410, "No eligible smart-link campaign is available")
    enforce_tracking_host(db, request, row.workspace_id, eligible[0][1].advertiser_id)
    choice = secrets.randbelow(sum(weights[child.id] for child, _ in eligible))
    selected = eligible[-1][0]
    for link, _ in eligible:
        choice -= weights[link.id]
        if choice < 0:
            selected = link
            break
    from app.core.config import get_settings
    from app.tenancy.service import company_context

    company = company_context.get()
    base = get_settings().public_base_url.rstrip("/") + ("/c/" + company.slug if company else "")
    query = {
        k: v
        for k, v in request.query_params.items()
        if k in TRACKED_PARAMETERS | {"sub_id", "url"} and len(v) <= 2048
    }
    return RedirectResponse(
        base + "/t/" + selected.id + ("?" + urlencode(query) if query else ""), status_code=302
    )
