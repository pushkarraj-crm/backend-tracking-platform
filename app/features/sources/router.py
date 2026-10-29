"""Blocked traffic sources: traffic an account refuses to pay for is rejected and logged as fraud."""

from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, model_validator
from sqlalchemy import or_, select

from app.core.security import DB, Actor
from app.core.services import audit
from app.permissions.resources import entitled, owned
from app.permissions.roles import staff_only
from app.persistence.models import FraudEvent, Offer, User
from app.persistence.network_models import BlockedSource
from app.shared.schemas import ActiveUpdate, StrictModel

router = APIRouter(tags=["Blocked traffic sources"])

# Common Indian cashback/coupon sites; accounts add others as custom domains.
CASHBACK_DOMAINS = [
    "cashkaro.com",
    "earnkaro.com",
    "grabon.in",
    "coupondunia.in",
    "desidime.com",
    "gopaisa.com",
    "zingoy.com",
    "paisawapas.com",
    "couponzguru.com",
    "freekaamaal.com",
    "magicpin.in",
]
MESSAGING_DOMAINS = [
    "t.me",
    "telegram.me",
    "telegram.org",
    "wa.me",
    "whatsapp.com",
]
PRESETS = {
    "google_ads": "Google ads (gclid, gbraid, wbraid)",
    "meta_ads": "Facebook / Instagram ads (fbclid)",
    "cashback_sites": "Cashback and coupon sites",
    "messaging_apps": "Telegram and WhatsApp",
    "no_referrer": "Traffic with no referring site",
    "custom_domain": "Referring site",
    "custom_source": "source / utm_source value",
}


def referrer_domain(request):
    """Hostname of the referring page only; paths and queries are never stored."""
    try:
        host = urlsplit(request.headers.get("referer", "")[:2048]).hostname or ""
    except ValueError:
        host = ""
    return host.lower().removeprefix("www.")[:253]


def on_domain(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


def matches(row, attributes):
    referrer = attributes.get("referrer")
    known = referrer is not None  # clicks recorded before referrer capture have no key
    sources = {str(attributes.get(k, "")).strip().lower() for k in ("source", "utm_source")} - {""}
    return {
        "google_ads": any(attributes.get(k) for k in ("gclid", "gbraid", "wbraid")),
        "meta_ads": bool(attributes.get("fbclid")),
        "cashback_sites": known and on_domain(referrer, CASHBACK_DOMAINS),
        "messaging_apps": (known and on_domain(referrer, MESSAGING_DOMAINS))
        or bool(sources & {"telegram", "whatsapp"}),
        "no_referrer": known and referrer == "",
        "custom_domain": known and on_domain(referrer, [row.value]),
        "custom_source": row.value in sources,
    }.get(row.preset, False)


def blocked_preset(db, offer, attributes):
    """The first active block that applies to this campaign's traffic, or None."""
    rows = db.scalars(
        select(BlockedSource)
        .where(
            BlockedSource.workspace_id == offer.workspace_id,
            BlockedSource.active.is_(True),
            or_(BlockedSource.offer_id.is_(None), BlockedSource.offer_id == offer.id),
        )
        .order_by(BlockedSource.id)
        .limit(200)
    )
    return next((row.preset for row in rows if matches(row, attributes)), None)


class BlockBody(StrictModel):
    preset: Literal[
        "google_ads",
        "meta_ads",
        "cashback_sites",
        "messaging_apps",
        "no_referrer",
        "custom_domain",
        "custom_source",
    ]
    value: str = Field(default="", max_length=253)
    campaign_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def valid(self):
        self.value = self.value.strip().lower()
        if self.preset == "custom_domain":
            self.value = self.value.removeprefix("https://").removeprefix("http://")
            self.value = self.value.split("/")[0].removeprefix("www.")
            if "." not in self.value or any(c in self.value for c in " ?#@:"):
                raise ValueError("Enter a domain such as example.com")
        elif self.preset == "custom_source":
            if not self.value or len(self.value) > 256:
                raise ValueError("Enter the source value to block")
        elif self.value:
            raise ValueError("Presets do not take a value")
        return self


def output(row, names):
    return {
        "id": row.id,
        "preset": row.preset,
        "label": PRESETS[row.preset],
        "value": row.value,
        "campaign_id": row.offer_id,
        "campaign_name": names.get(row.offer_id),
        "active": row.active,
        "created_at": row.created_at,
    }


def campaign_names(db, workspace_id):
    rows = db.execute(select(Offer.id, Offer.name).where(Offer.workspace_id == workspace_id))
    return {offer_id: name for offer_id, name in rows}


@router.get("/blocked-sources")
def blocked_sources(db: DB, user: Actor):
    staff_only(user)
    entitled(db, user)
    names = campaign_names(db, user.workspace_id)
    rows = db.scalars(
        select(BlockedSource)
        .where(BlockedSource.workspace_id == user.workspace_id)
        .order_by(BlockedSource.created_at.desc(), BlockedSource.id)
        .limit(200)
    )
    return {
        "presets": PRESETS,
        "cashback_domains": CASHBACK_DOMAINS,
        "messaging_domains": MESSAGING_DOMAINS,
        "blocks": [output(row, names) for row in rows],
    }


@router.post("/blocked-sources", status_code=201)
def add_block(body: BlockBody, db: DB, user: Actor):
    staff_only(user)
    entitled(db, user)
    if body.campaign_id:
        owned(db, Offer, body.campaign_id, user)
    row = BlockedSource(
        workspace_id=user.workspace_id,
        offer_id=body.campaign_id,
        preset=body.preset,
        value=body.value,
    )
    db.add(row)
    db.flush()
    audit(db, user, "blocked_source.created", row.id, f"{body.preset} {body.value}"[:200])
    db.commit()
    return output(row, campaign_names(db, user.workspace_id))


@router.patch("/blocked-sources/{block_id}")
def block_active(block_id: str, body: ActiveUpdate, db: DB, user: Actor):
    staff_only(user)
    row = owned(db, BlockedSource, block_id, user)
    row.active = body.active
    audit(db, user, "blocked_source.active", row.id, str(body.active))
    db.commit()
    return output(row, campaign_names(db, user.workspace_id))


@router.get("/fraud/partner-events")
def partner_events(db: DB, user: Actor, limit: int = Query(50, ge=1, le=100)):
    """Partners see which of their traffic was rejected and why, without visitor data."""
    if user.role not in {"affiliate", "publisher"}:
        raise HTTPException(403, "Partner access required")
    team = select(User.id).where(
        User.workspace_id == user.workspace_id,
        (User.id == user.id) | (User.parent_user_id == user.id),
    )
    rows = db.execute(
        select(FraudEvent, Offer.name)
        .join(Offer, Offer.id == FraudEvent.offer_id)
        .where(FraudEvent.workspace_id == user.workspace_id, FraudEvent.publisher_id.in_(team))
        .order_by(FraudEvent.created_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": e.id,
            "campaign_name": name,
            "reason": reason_label(e.reason),
            "created_at": e.created_at,
        }
        for e, name in rows
    ]


def reason_label(reason):
    preset = reason.removeprefix("blocked_source:")
    return f"Blocked source: {PRESETS[preset]}" if preset in PRESETS else reason.replace("_", " ")
