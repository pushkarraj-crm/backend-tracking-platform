"""Influencer accounts: brand deals, a link per post or spot, manual sales and the creator dashboard."""

import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, model_validator
from sqlalchemy import func, select

from app.core.security import DB, Actor, passwords
from app.core.services import audit
from app.features.tracking.service import link_url
from app.permissions.resources import entitled, owned
from app.persistence.models import Click, Conversion, Offer, TrackingLink, User, Workspace
from app.persistence.network_models import ManualResult, Placement
from app.shared.schemas import StrictModel

router = APIRouter(tags=["Influencer accounts"])

PLATFORMS = Literal[
    "instagram", "youtube", "facebook", "x", "telegram", "whatsapp", "blog", "other"
]


def creator_workspace(db, user):
    workspace = db.get(Workspace, user.workspace_id)
    if workspace.account_type != "influencer":
        raise HTTPException(404, "Available to influencer accounts only")
    if user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Account access required")
    entitled(db, user)
    return workspace


def house_user(db, workspace, key, role, name):
    """Login-less records: brands are advertisers and the creator is the partner on their own links."""
    email = f"{key}@{workspace.id}.house.invalid"
    record = db.scalar(select(User).where(User.workspace_id == workspace.id, User.email == email))
    if not record:
        record = User(
            workspace_id=workspace.id,
            email=email,
            # Nobody knows this password, so the record cannot sign in.
            password_hash=passwords.hash(secrets.token_urlsafe(32)),
            role=role,
            full_name=name,
            email_verified=True,
        )
        db.add(record)
        db.flush()
    return record


class BrandBody(StrictModel):
    name: str = Field(min_length=1, max_length=120)


@router.get("/brands")
def brands(db: DB, user: Actor):
    workspace = creator_workspace(db, user)
    rows = db.scalars(
        select(User)
        .where(
            User.workspace_id == workspace.id,
            User.role == "advertiser",
            User.email.like("brand-%.house.invalid"),
        )
        .order_by(User.full_name)
        .limit(500)
    )
    return [{"id": r.id, "name": r.full_name, "active": r.active} for r in rows]


@router.post("/brands", status_code=201)
def add_brand(body: BrandBody, db: DB, user: Actor):
    workspace = creator_workspace(db, user)
    brand = house_user(db, workspace, "brand-" + secrets.token_hex(6), "advertiser", body.name)
    audit(db, user, "brand.created", brand.id, body.name[:200])
    db.commit()
    return {"id": brand.id, "name": brand.full_name, "active": brand.active}


class PlacementBody(StrictModel):
    campaign_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=120)
    platform: PLATFORMS = "instagram"


def placement_output(db, base, row, link):
    return {
        "id": row.id,
        "campaign_id": link.offer_id,
        "name": row.name,
        "platform": row.platform,
        "sub_id": row.sub_id,
        "url": link_url(db, base, link) + "&sub_id=" + row.sub_id,
        "created_at": row.created_at,
    }


@router.post("/placements", status_code=201)
def add_placement(body: PlacementBody, db: DB, user: Actor):
    from app.features.domains.router import link_base

    workspace = creator_workspace(db, user)
    offer = owned(db, Offer, body.campaign_id, user)
    if not offer.active:
        raise HTTPException(400, "Activate the brand deal before adding links")
    creator = house_user(db, workspace, "creator", "affiliate", workspace.name)
    link = db.scalar(
        select(TrackingLink).where(
            TrackingLink.workspace_id == workspace.id,
            TrackingLink.offer_id == offer.id,
            TrackingLink.affiliate_id == creator.id,
        )
    )
    if not link:
        link = TrackingLink(workspace_id=workspace.id, offer_id=offer.id, affiliate_id=creator.id)
        db.add(link)
        db.flush()
    slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")[:60] or "link"
    # A short suffix keeps two posts with the same name apart.
    row = Placement(
        workspace_id=workspace.id,
        link_id=link.id,
        name=body.name,
        platform=body.platform,
        sub_id=f"{body.platform}-{slug}-{secrets.token_hex(2)}",
    )
    db.add(row)
    db.flush()
    result = placement_output(db, link_base(db, user, None), row, link)
    audit(db, user, "placement.created", row.id, body.name[:200])
    db.commit()
    return result


@router.get("/placements")
def placements(db: DB, user: Actor):
    from app.features.domains.router import link_base

    workspace = creator_workspace(db, user)
    base = link_base(db, user, None)
    rows = db.execute(
        select(Placement, TrackingLink)
        .join(TrackingLink, TrackingLink.id == Placement.link_id)
        .where(Placement.workspace_id == workspace.id)
        .order_by(Placement.created_at.desc(), Placement.id)
        .limit(500)
    ).all()
    result = [placement_output(db, base, row, link) for row, link in rows]
    db.commit()  # keeps public link numbers assigned while building URLs
    return result


class ManualBody(StrictModel):
    campaign_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    period_start: date
    period_end: date
    sales: int = Field(ge=0, le=10_000_000)
    earnings: Decimal = Field(ge=0, max_digits=14, decimal_places=4)
    note: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def valid(self):
        if self.period_end < self.period_start:
            raise ValueError("The end date must be on or after the start date")
        return self


def manual_output(row, offer):
    return {
        "id": row.id,
        "campaign_id": row.offer_id,
        "campaign_name": offer.name,
        "currency": offer.currency,
        "period_start": row.period_start,
        "period_end": row.period_end,
        "sales": row.sales,
        "earnings": str(row.earnings),
        "note": row.note,
    }


@router.post("/manual-results", status_code=201)
def add_manual_result(body: ManualBody, db: DB, user: Actor):
    workspace = creator_workspace(db, user)
    offer = owned(db, Offer, body.campaign_id, user)
    row = ManualResult(
        workspace_id=workspace.id,
        offer_id=offer.id,
        **body.model_dump(exclude={"campaign_id"}),
    )
    db.add(row)
    db.flush()
    audit(db, user, "manual_result.created", row.id, f"{body.sales} sales")
    db.commit()
    return manual_output(row, offer)


@router.get("/manual-results")
def manual_results(db: DB, user: Actor):
    workspace = creator_workspace(db, user)
    rows = db.execute(
        select(ManualResult, Offer)
        .join(Offer, Offer.id == ManualResult.offer_id)
        .where(ManualResult.workspace_id == workspace.id)
        .order_by(ManualResult.period_start.desc(), ManualResult.id)
        .limit(500)
    ).all()
    return [manual_output(row, offer) for row, offer in rows]


@router.get("/influencer/summary")
def summary(db: DB, user: Actor, start: date | None = Query(None), end: date | None = Query(None)):
    """Clicks, sales and earnings by brand deal, placement and platform for a date range (UTC days)."""
    workspace = creator_workspace(db, user)
    end = end or datetime.now(timezone.utc).date()
    start = start or end - timedelta(days=29)
    if start > end or (end - start).days > 366:
        raise HTTPException(422, "Use a start date on or before the end date, within one year")
    since = datetime.combine(start, datetime.min.time(), timezone.utc)
    until = datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc)
    ws = workspace.id

    def blank():
        return {"clicks": 0, "sales": 0, "earnings": Decimal(0)}

    deals = {
        o.id: {"id": o.id, "name": o.name, "currency": o.currency, "brand": name, **blank()}
        for o, name in db.execute(
            select(Offer, User.full_name)
            .join(User, User.id == Offer.advertiser_id)
            .where(Offer.workspace_id == ws)
        )
    }
    spots = {
        (link.offer_id, p.sub_id): {
            "name": p.name,
            "platform": p.platform,
            "campaign_id": link.offer_id,
            **blank(),
        }
        for p, link in db.execute(
            select(Placement, TrackingLink)
            .join(TrackingLink, TrackingLink.id == Placement.link_id)
            .where(Placement.workspace_id == ws)
        )
    }
    clicks = db.execute(
        select(TrackingLink.offer_id, Click.sub_id, func.count())
        .join(TrackingLink, TrackingLink.id == Click.link_id)
        .where(Click.workspace_id == ws, Click.created_at >= since, Click.created_at < until)
        .group_by(TrackingLink.offer_id, Click.sub_id)
    )
    for offer_id, sub_id, count in clicks:
        for bucket in (deals.get(offer_id), spots.get((offer_id, sub_id))):
            if bucket:
                bucket["clicks"] += count
    sales = db.execute(
        select(Conversion.offer_id, Click.sub_id, func.count(), func.sum(Conversion.payout))
        .join(Click, Click.id == Conversion.click_id)
        .where(
            Conversion.workspace_id == ws,
            Conversion.status != "rejected",
            Conversion.created_at >= since,
            Conversion.created_at < until,
        )
        .group_by(Conversion.offer_id, Click.sub_id)
    )
    for offer_id, sub_id, count, amount in sales:
        for bucket in (deals.get(offer_id), spots.get((offer_id, sub_id))):
            if bucket:
                bucket["sales"] += count
                bucket["earnings"] += amount or 0
    manual = db.execute(
        select(ManualResult.offer_id, func.sum(ManualResult.sales), func.sum(ManualResult.earnings))
        .where(
            ManualResult.workspace_id == ws,
            ManualResult.period_start >= start,
            ManualResult.period_start <= end,
        )
        .group_by(ManualResult.offer_id)
    )
    for offer_id, count, amount in manual:
        if offer_id in deals:
            deals[offer_id]["reported_sales"] = int(count or 0)
            deals[offer_id]["reported_earnings"] = str(amount or 0)
            deals[offer_id]["sales"] += int(count or 0)
            deals[offer_id]["earnings"] += amount or 0
    platforms = {}
    for spot in spots.values():
        total = platforms.setdefault(spot["platform"], {"platform": spot["platform"], **blank()})
        for key in ("clicks", "sales", "earnings"):
            total[key] += spot[key]

    def finish(row):
        row["conversion_rate"] = (
            round(row["sales"] * 100 / row["clicks"], 2) if row["clicks"] else 0
        )
        row["earnings"] = str(row["earnings"])
        return row

    by = lambda rows: sorted((finish(r) for r in rows), key=lambda r: -Decimal(r["earnings"]))  # noqa: E731
    return {
        "start": start,
        "end": end,
        "deals": by(deals.values()),
        "placements": by(spots.values()),
        "platforms": by(platforms.values()),
    }
