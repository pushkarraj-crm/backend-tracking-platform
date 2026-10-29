import hashlib
import hmac
import ipaddress
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Literal

import pycountry
from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, model_validator
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.security import DB, Actor
from app.core.services import audit
from app.permissions.roles import staff_only
from app.persistence.models import FraudCounter, FraudEvent, Offer, OfferCounter, identifier
from app.shared.schemas import StrictModel

router = APIRouter(tags=["Targeting and fraud"])


class Rules(StrictModel):
    countries: list[str] = Field(default_factory=list, max_length=249)
    devices: list[Literal["desktop", "mobile", "tablet", "unknown"]] = Field(
        default_factory=list, max_length=4
    )
    operating_systems: list[Literal["android", "ios", "windows", "macos", "linux", "other"]] = (
        Field(default_factory=list, max_length=6)
    )
    languages: list[str] = Field(default_factory=list, max_length=50)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    utc_hours: list[int] = Field(default_factory=list, max_length=24)
    daily_clicks: int | None = Field(default=None, ge=1)
    total_clicks: int | None = Field(default=None, ge=1)
    daily_conversions: int | None = Field(default=None, ge=1)
    total_conversions: int | None = Field(default=None, ge=1)
    daily_revenue: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=4)
    total_revenue: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=4)
    block_bots: bool = True
    denied_networks: list[str] = Field(default_factory=list, max_length=100)
    clicks_per_ip_minute: int = Field(default=120, ge=1, le=10000)
    minimum_conversion_seconds: int = Field(default=0, ge=0, le=3600)
    # Repeat clicks from the same visitor on the same link reuse their earlier click.
    unique_click_hours: int = Field(default=0, ge=0, le=720)

    @model_validator(mode="after")
    def validate_rules(self):
        if any(pycountry.countries.get(alpha_2=c) is None for c in self.countries):
            raise ValueError("Invalid country code")
        if any(not re.fullmatch(r"[a-z]{2,3}", language) for language in self.languages):
            raise ValueError("Use language codes such as en")
        if any(hour < 0 or hour > 23 for hour in self.utc_hours):
            raise ValueError("UTC hours must be 0–23")
        for value in (self.starts_at, self.ends_at):
            if value and value.tzinfo is None:
                raise ValueError("Schedule requires timezone-aware dates")
        if self.starts_at and self.ends_at and self.starts_at >= self.ends_at:
            raise ValueError("Invalid schedule")
        for network in self.denied_networks:
            ipaddress.ip_network(network, strict=False)
        return self


@router.put("/offers/{offer_id}/rules")
def set_rules(offer_id: str, body: Rules, db: DB, user: Actor):
    from app.permissions.resources import entitled, owned

    entitled(db, user)
    offer = owned(db, Offer, offer_id, user)
    if user.role == "advertiser":
        if offer.advertiser_id != user.id:
            raise HTTPException(404, "Campaign not found")
    else:
        staff_only(user)
    offer.tracking_rules = body.model_dump(mode="json")
    audit(db, user, "offer.rules", offer.id)
    db.commit()
    return offer.tracking_rules


@router.get("/offers/{offer_id}/rules")
def get_rules(offer_id: str, db: DB, user: Actor):
    from app.permissions.resources import owned

    offer = owned(db, Offer, offer_id, user)
    if user.role == "advertiser":
        if offer.advertiser_id != user.id:
            raise HTTPException(404, "Campaign not found")
    else:
        staff_only(user)
    return Rules.model_validate(offer.tracking_rules).model_dump(mode="json")


def insert_once(db, model, values, columns):
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(insert(model).values(**values).on_conflict_do_nothing(index_elements=columns))


def reserve(db, offer, kind, at, revenue=None):
    revenue = offer.revenue if revenue is None else revenue
    rules = Rules.model_validate(offer.tracking_rules)
    for period in (at.astimezone(timezone.utc).date().isoformat(), "total"):
        insert_once(
            db,
            OfferCounter,
            dict(
                id=identifier(),
                workspace_id=offer.workspace_id,
                offer_id=offer.id,
                period=period,
                clicks=0,
                conversions=0,
                revenue=0,
            ),
            ["workspace_id", "offer_id", "period"],
        )
        criteria = [
            OfferCounter.workspace_id == offer.workspace_id,
            OfferCounter.offer_id == offer.id,
            OfferCounter.period == period,
        ]
        limit = getattr(rules, ("total_" if period == "total" else "daily_") + kind)
        column = getattr(OfferCounter, kind)
        if limit is not None:
            criteria.append(column < limit)
        values = {kind: column + 1}
        if kind == "conversions":
            budget = rules.total_revenue if period == "total" else rules.daily_revenue
            if budget is not None:
                criteria.append(OfferCounter.revenue + revenue <= budget)
            values["revenue"] = OfferCounter.revenue + revenue
        if db.execute(update(OfferCounter).where(*criteria).values(**values)).rowcount != 1:
            db.rollback()
            raise HTTPException(409, "Offer cap reached")


@lru_cache
def geo_reader():
    path = get_settings().geoip_database_path
    if not path:
        return None
    import geoip2.database

    return geoip2.database.Reader(path)


def context(request):
    address = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "")[:1024].lower()
    os_name = (
        "android"
        if "android" in ua
        else "ios"
        if re.search("iphone|ipad|ipod", ua)
        else "windows"
        if "windows" in ua
        else "macos"
        if "mac os" in ua
        else "linux"
        if "linux" in ua
        else "other"
    )
    device = (
        "tablet"
        if "ipad" in ua or ("android" in ua and "mobile" not in ua)
        else "mobile"
        if re.search("mobile|iphone|ipod", ua)
        else "desktop"
        if os_name in {"windows", "macos", "linux"}
        else "unknown"
    )
    language = (
        request.headers.get("accept-language", "").split(",")[0].split("-")[0].strip().lower()
    )
    language = language if re.fullmatch("[a-z]{2,3}", language) else None
    country = None
    reader = geo_reader()
    if reader:
        try:
            country = reader.country(address).country.iso_code
        except Exception:
            country = None
    secret = get_settings().encryption_key or get_settings().jwt_secret
    digest = hmac.new(secret.encode(), address.encode(), hashlib.sha256).hexdigest()
    return (
        dict(
            country=country,
            device=device,
            operating_system=os_name,
            language=language,
            ip_hash=digest,
        ),
        address,
        ua,
    )


def reject_fraud(db, offer, reason, score, ip_hash=None, click_id=None, publisher_id=None):
    hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    # One alert per campaign per hour; the Fraud page has every event.
    if not db.scalar(
        select(FraudEvent.id)
        .where(
            FraudEvent.workspace_id == offer.workspace_id,
            FraudEvent.offer_id == offer.id,
            FraudEvent.created_at >= hour_ago,
        )
        .limit(1)
    ):
        from app.features.alerts.router import alert

        alert(
            db,
            offer.workspace_id,
            "fraud",
            f"🚫 {offer.name}: traffic blocked ({reason.replace('_', ' ')}). "
            "More blocks in the next hour are on the Fraud page.",
        )
    db.add(
        FraudEvent(
            workspace_id=offer.workspace_id,
            offer_id=offer.id,
            reason=reason,
            score=score,
            ip_hash=ip_hash,
            click_id=click_id,
            publisher_id=publisher_id,
        )
    )
    db.commit()
    raise HTTPException(403, "Traffic rejected by fraud policy")


def check_click(db, offer, request):
    rules = Rules.model_validate(offer.tracking_rules)
    metadata, address, ua = context(request)
    at = datetime.now(timezone.utc)
    if (
        (rules.starts_at and at < rules.starts_at)
        or (rules.ends_at and at >= rules.ends_at)
        or (rules.utc_hours and at.hour not in rules.utc_hours)
    ):
        raise HTTPException(410, "Offer is outside its schedule")
    for key, allowed in (
        ("country", rules.countries),
        ("device", rules.devices),
        ("operating_system", rules.operating_systems),
        ("language", rules.languages),
    ):
        if allowed and metadata[key] not in allowed:
            raise HTTPException(403, "Traffic does not match offer targeting")
    if rules.block_bots and (
        not ua or re.search(r"bot\b|crawler|spider|headless|curl/|python-requests", ua)
    ):
        reject_fraud(db, offer, "bot_user_agent", 90, metadata["ip_hash"])
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        ip = None
    if ip and any(
        ip in ipaddress.ip_network(network, strict=False) for network in rules.denied_networks
    ):
        reject_fraud(db, offer, "blocked_ip_network", 100, metadata["ip_hash"])
    minute = int(at.timestamp()) // 60
    insert_once(
        db,
        FraudCounter,
        dict(
            id=identifier(),
            workspace_id=offer.workspace_id,
            offer_id=offer.id,
            ip_hash=metadata["ip_hash"],
            minute=minute,
            count=0,
        ),
        ["workspace_id", "offer_id", "ip_hash", "minute"],
    )
    count = db.scalar(
        update(FraudCounter)
        .where(
            FraudCounter.workspace_id == offer.workspace_id,
            FraudCounter.offer_id == offer.id,
            FraudCounter.ip_hash == metadata["ip_hash"],
            FraudCounter.minute == minute,
        )
        .values(count=FraudCounter.count + 1)
        .returning(FraudCounter.count)
    )
    if count > rules.clicks_per_ip_minute:
        reject_fraud(db, offer, "click_velocity", 80, metadata["ip_hash"])
    return metadata


@router.get("/fraud/events")
def fraud_events(db: DB, user: Actor, limit: int = Query(default=50, ge=1, le=100)):
    staff_only(user)
    return [
        {
            key: getattr(row, key)
            for key in ("id", "offer_id", "click_id", "reason", "score", "reviewed", "created_at")
        }
        for row in db.scalars(
            select(FraudEvent)
            .where(FraudEvent.workspace_id == user.workspace_id)
            .order_by(FraudEvent.created_at.desc())
            .limit(limit)
        )
    ]


@router.post("/fraud/events/{event_id}/review")
def review(event_id: str, db: DB, user: Actor):
    from app.permissions.resources import owned

    staff_only(user)
    event = owned(db, FraudEvent, event_id, user)
    event.reviewed = True
    audit(db, user, "fraud.reviewed", event.id)
    db.commit()
    return {"reviewed": True}
