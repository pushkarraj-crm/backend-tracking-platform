import secrets

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import DB, Actor, key_hash
from app.core.services import audit, quota
from app.features.campaigns.presentation import output_offer
from app.permissions.resources import entitled, owned, visible_users
from app.permissions.roles import staff_only
from app.persistence.models import Offer, TrackingLink, User
from app.shared.schemas import ActiveUpdate, OfferCreate

router = APIRouter()
settings = get_settings()


@router.post("/offers", status_code=201)
def create_offer(body: OfferCreate, db: DB, user: Actor):
    if user.role == "advertiser":
        if body.advertiser_id and body.advertiser_id != user.id:
            raise HTTPException(403, "Cannot submit offers for another advertiser")
        body.advertiser_id = user.id
    else:
        staff_only(user)
    if body.advertiser_id and owned(db, User, body.advertiser_id, user).role != "advertiser":
        raise HTTPException(400, "Offer owner must be an advertiser")
    quota(db, user, Offer, "offers")
    key = secrets.token_urlsafe(32)
    offer = Offer(
        workspace_id=user.workspace_id,
        **body.model_dump(exclude={"destination_url"}),
        destination_url=str(body.destination_url),
        postback_key_hash=key_hash(key),
        active=user.role != "advertiser",
    )
    db.add(offer)
    db.flush()
    audit(db, user, "offer.created", offer.id)
    db.commit()
    return {**output_offer(offer), "postback_key": key}


@router.get("/offers")
def list_offers(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    entitled(db, user)
    query = select(Offer).where(Offer.workspace_id == user.workspace_id)
    if user.role in {"affiliate", "publisher"}:
        query = query.where(
            Offer.id.in_(
                select(TrackingLink.offer_id).where(
                    TrackingLink.workspace_id == user.workspace_id,
                    TrackingLink.affiliate_id.in_(visible_users(user)),
                )
            )
        )
    elif user.role == "advertiser":
        query = query.where(Offer.advertiser_id == user.id)
    else:
        staff_only(user)
    rows = [
        output_offer(o) for o in db.scalars(query.order_by(Offer.id).offset(offset).limit(limit))
    ]
    if user.role in {"affiliate", "publisher"}:
        for row in rows:
            row.pop("revenue")
            row.pop("destination_url")
    elif user.role == "advertiser":
        for row in rows:
            row.pop("payout")
    return rows


@router.patch("/offers/{offer_id}")
def set_offer_active(offer_id: str, body: ActiveUpdate, db: DB, user: Actor):
    entitled(db, user)
    staff_only(user)
    offer = owned(db, Offer, offer_id, user)
    offer.active = body.active
    audit(db, user, "offer.active", offer.id, str(body.active))
    db.commit()
    return output_offer(offer)


@router.post("/offers/{offer_id}/rotate-postback-key")
def rotate_key(offer_id: str, db: DB, user: Actor):
    entitled(db, user)
    offer = owned(db, Offer, offer_id, user)
    if user.role == "advertiser":
        if offer.advertiser_id != user.id:
            raise HTTPException(404, "Resource not found")
    else:
        staff_only(user)
    key = secrets.token_urlsafe(32)
    offer.postback_key_hash = key_hash(key)
    audit(db, user, "offer.rotate_key", offer.id, "Previous key invalidated")
    db.commit()
    return {"postback_key": key}
