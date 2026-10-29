"""Coupon codes: managed per campaign and partner, and used to credit sales that carry no click."""

import csv
import io
import re
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import Field, ValidationError, model_validator
from sqlalchemy import func, select

from app.core.security import DB, Actor
from app.core.services import audit
from app.permissions.resources import entitled, owned, visible_users
from app.permissions.roles import staff_only
from app.persistence.models import (
    CampaignApplication,
    Click,
    Conversion,
    Offer,
    TrackingLink,
    User,
    Workspace,
)
from app.persistence.network_models import Coupon
from app.shared.schemas import StrictModel

router = APIRouter(tags=["Coupons"])
CODE = re.compile(r"^[A-Z0-9][A-Z0-9_\-]{1,63}$")


def normalize(code):
    return str(code or "").strip().upper()


def coupon_sub_id(code):
    return ("coupon:" + code)[:100]


def usable_coupon(db, workspace_id, code, today=None):
    """The active coupon for this code if it is within its dates and quota, else None."""
    today = today or datetime.now(timezone.utc).date()
    coupon = db.scalar(
        select(Coupon)
        .where(Coupon.workspace_id == workspace_id, Coupon.code == normalize(code))
        .with_for_update()
    )
    if (
        not coupon
        or not coupon.active
        or (coupon.starts_on and today < coupon.starts_on)
        or (coupon.ends_on and today > coupon.ends_on)
        or (coupon.quota is not None and coupon.uses >= coupon.quota)
    ):
        return None
    return coupon


def coupon_click(db, coupon):
    """A click record standing for a coupon sale, so pricing, caps and reports work unchanged."""
    link = db.scalar(
        select(TrackingLink).where(
            TrackingLink.workspace_id == coupon.workspace_id,
            TrackingLink.offer_id == coupon.offer_id,
            TrackingLink.affiliate_id == coupon.partner_id,
        )
    )
    if not link:
        link = TrackingLink(
            workspace_id=coupon.workspace_id,
            offer_id=coupon.offer_id,
            affiliate_id=coupon.partner_id,
        )
        db.add(link)
        db.flush()
    click = Click(
        workspace_id=coupon.workspace_id,
        link_id=link.id,
        sub_id=coupon_sub_id(coupon.code),
        attributes={"coupon": coupon.code, "attribution": "coupon"},
    )
    db.add(click)
    db.flush()
    return click


class CouponBody(StrictModel):
    campaign_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    partner_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    code: str = Field(min_length=2, max_length=64)
    description: str = Field(default="", max_length=300)
    starts_on: date | None = None
    ends_on: date | None = None
    quota: int | None = Field(default=None, gt=0, le=10_000_000)

    @model_validator(mode="after")
    def valid(self):
        self.code = normalize(self.code)
        if not CODE.fullmatch(self.code):
            raise ValueError("Codes use letters, numbers, - and _ only")
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            raise ValueError("The end date must be on or after the start date")
        return self


def coupon_partner(db, user, offer, partner_id):
    """Creators' codes are their own; other accounts name a partner allowed to promote the campaign."""
    workspace = db.get(Workspace, user.workspace_id)
    if workspace.account_type == "influencer":
        from app.features.influencer.router import house_user

        return house_user(db, workspace, "creator", "affiliate", workspace.name).id
    if not partner_id:
        raise HTTPException(422, "Select the partner who will share this code")
    partner = owned(db, User, partner_id, user)
    if partner.role not in {"affiliate", "publisher"} or not partner.active:
        raise HTTPException(422, "Select an active partner")
    if partner.role == "publisher" and not db.scalar(
        select(CampaignApplication.id).where(
            CampaignApplication.workspace_id == user.workspace_id,
            CampaignApplication.offer_id == offer.id,
            CampaignApplication.publisher_id == partner.id,
            CampaignApplication.status == "approved",
        )
    ):
        raise HTTPException(422, "This partner is not approved for the campaign")
    return partner.id


def stats(db, workspace_id):
    """Coupon-attributed conversions and earnings per code (rejected conversions excluded)."""
    rows = db.execute(
        select(
            Click.sub_id,
            func.count(Conversion.id),
            func.coalesce(func.sum(Conversion.payout), 0),
        )
        .join(Conversion, Conversion.click_id == Click.id)
        .where(
            Click.workspace_id == workspace_id,
            Click.sub_id.like("coupon:%"),
            Conversion.status != "rejected",
        )
        .group_by(Click.sub_id)
    )
    return {sub: (count, total) for sub, count, total in rows}


def output(row, offer, partner, figures):
    count, total = figures.get(coupon_sub_id(row.code), (0, 0))
    return {
        "id": row.id,
        "code": row.code,
        "campaign_id": row.offer_id,
        "campaign_name": offer.name,
        "currency": offer.currency,
        "partner_id": row.partner_id,
        "partner_name": partner.full_name or partner.email,
        "description": row.description,
        "starts_on": row.starts_on,
        "ends_on": row.ends_on,
        "quota": row.quota,
        "uses": row.uses,
        "conversions": count,
        "earnings": str(total),
        "active": row.active,
    }


def listing(db, workspace_id, partner_ids=None, coupon_id=None):
    query = (
        select(Coupon, Offer, User)
        .join(Offer, Offer.id == Coupon.offer_id)
        .join(User, User.id == Coupon.partner_id)
        .where(Coupon.workspace_id == workspace_id)
        .order_by(Coupon.created_at.desc(), Coupon.id)
        .limit(1000)
    )
    if partner_ids is not None:
        query = query.where(Coupon.partner_id.in_(partner_ids))
    if coupon_id:
        query = query.where(Coupon.id == coupon_id)
    figures = stats(db, workspace_id)
    return [output(c, o, u, figures) for c, o, u in db.execute(query)]


@router.get("/coupons")
def coupons(db: DB, user: Actor):
    staff_only(user)
    entitled(db, user)
    return listing(db, user.workspace_id)


@router.get("/coupons/mine")
def my_coupons(db: DB, user: Actor):
    if user.role not in {"affiliate", "publisher"}:
        raise HTTPException(403, "Partner access required")
    entitled(db, user)
    return listing(db, user.workspace_id, visible_users(user))


def create(db, user, body):
    offer = owned(db, Offer, body.campaign_id, user)
    partner_id = coupon_partner(db, user, offer, body.partner_id)
    if db.scalar(
        select(Coupon.id).where(Coupon.workspace_id == user.workspace_id, Coupon.code == body.code)
    ):
        raise HTTPException(409, f"Code {body.code} already exists in this account")
    row = Coupon(
        workspace_id=user.workspace_id,
        offer_id=offer.id,
        partner_id=partner_id,
        **body.model_dump(exclude={"campaign_id", "partner_id"}),
    )
    db.add(row)
    db.flush()
    return row


@router.post("/coupons", status_code=201)
def add_coupon(body: CouponBody, db: DB, user: Actor):
    staff_only(user)
    entitled(db, user)
    row = create(db, user, body)
    audit(db, user, "coupon.created", row.id, row.code)
    db.commit()
    return listing(db, user.workspace_id, coupon_id=row.id)[0]


class CouponUpdate(StrictModel):
    active: bool | None = None
    quota: int | None = Field(default=None, gt=0, le=10_000_000)
    ends_on: date | None = None


@router.patch("/coupons/{coupon_id}")
def update_coupon(coupon_id: str, body: CouponUpdate, db: DB, user: Actor):
    staff_only(user)
    row = owned(db, Coupon, coupon_id, user)
    for name in body.model_fields_set:
        value = getattr(body, name)
        if name == "active" and value is None:
            continue
        setattr(row, name, value)
    audit(db, user, "coupon.updated", row.id, row.code)
    db.commit()
    return listing(db, user.workspace_id, coupon_id=row.id)[0]


class ImportBody(StrictModel):
    campaign_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    partner_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    # shortcut: bounded by the request size limit (roughly 2,000 short codes per upload).
    csv_content: str = Field(min_length=1, max_length=60000)
    dry_run: bool = True


@router.post("/coupons/import")
def import_coupons(body: ImportBody, db: DB, user: Actor):
    """CSV with a `code` column and optional `quota`, `starts_on`, `ends_on`, `description`."""
    staff_only(user)
    entitled(db, user)
    stream = csv.DictReader(io.StringIO(body.csv_content.lstrip("﻿")))
    fields = [f.strip().lower() for f in stream.fieldnames or []]
    allowed = {"code", "quota", "starts_on", "ends_on", "description"}
    if "code" not in fields or set(fields) - allowed:
        raise HTTPException(
            422, "Use a code column and optionally quota, starts_on, ends_on, description"
        )
    errors, created, seen = [], 0, set()
    for number, raw in enumerate(stream, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        try:
            coupon = CouponBody(
                campaign_id=body.campaign_id,
                partner_id=body.partner_id,
                **{k: v for k, v in row.items() if v},
            )
            if coupon.code in seen:
                raise ValueError(f"Code {coupon.code} appears twice in the file")
            seen.add(coupon.code)
            create(db, user, coupon)
            created += 1
        except ValidationError as error:
            errors.append({"row": number, "message": "; ".join(e["msg"] for e in error.errors())})
        except (ValueError, HTTPException) as error:
            message = error.detail if isinstance(error, HTTPException) else str(error)
            errors.append({"row": number, "message": str(message)})
    result = {
        "dry_run": body.dry_run,
        "codes": created,
        "errors": errors,
        "can_import": not errors,
    }
    # One transaction: the preview is exact and an import with errors adds nothing.
    if body.dry_run or errors:
        db.rollback()
        if errors and not body.dry_run:
            raise HTTPException(422, {"message": "Fix the errors before importing", **result})
        return result
    audit(db, user, "coupon.imported", body.campaign_id, f"{created} codes")
    db.commit()
    return result
