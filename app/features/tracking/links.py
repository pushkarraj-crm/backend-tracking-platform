from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.security import DB, Actor
from app.core.services import audit
from app.features.tracking.service import link_url
from app.permissions.resources import entitled, owned, visible_users
from app.permissions.roles import staff_only
from app.persistence.models import Offer, TrackingLink, User
from app.shared.schemas import LinkCreate

router = APIRouter()


@router.post("/links", status_code=201)
def create_link(
    body: LinkCreate,
    db: DB,
    user: Actor,
    tracking_domain_id: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
):
    entitled(db, user)
    from app.features.domains.router import link_base

    base = link_base(db, user, tracking_domain_id)
    offer = owned(db, Offer, body.offer_id, user)
    affiliate = owned(db, User, body.affiliate_id, user)
    if user.role == "affiliate":
        if affiliate.role != "publisher" or affiliate.parent_user_id != user.id:
            raise HTTPException(403, "Can assign links only to your own publishers")
        assigned = db.scalar(
            select(TrackingLink.id).where(
                TrackingLink.workspace_id == user.workspace_id,
                TrackingLink.offer_id == offer.id,
                TrackingLink.affiliate_id == user.id,
            )
        )
        if not assigned:
            raise HTTPException(403, "Offer is not assigned to this affiliate")
    else:
        staff_only(user)
    if affiliate.role not in {"affiliate", "publisher"} or not affiliate.active or not offer.active:
        raise HTTPException(400, "An active offer and affiliate are required")
    link = TrackingLink(workspace_id=user.workspace_id, **body.model_dump())
    db.add(link)
    try:
        db.flush()
        audit(db, user, "link.created", link.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Tracking link already exists")
    url = link_url(db, base, link)
    db.commit()
    return {"id": link.id, "url": url}


@router.get("/links")
def list_links(
    db: DB,
    user: Actor,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    tracking_domain_id: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
):
    entitled(db, user)
    from app.features.domains.router import link_base

    base = link_base(db, user, tracking_domain_id)
    if user.role not in {"admin", "staff", "affiliate", "publisher"}:
        raise HTTPException(403, "Tracking link access required")
    query = select(TrackingLink).where(TrackingLink.workspace_id == user.workspace_id)
    if user.role in {"affiliate", "publisher"}:
        query = query.where(TrackingLink.affiliate_id.in_(visible_users(user)))
    rows = [
        {
            "id": link.id,
            "offer_id": link.offer_id,
            "affiliate_id": link.affiliate_id,
            "url": link_url(db, base, link),
        }
        for link in db.scalars(query.order_by(TrackingLink.id).offset(offset).limit(limit))
    ]
    db.commit()  # keeps public numbers assigned while building the URLs
    return rows


@router.get("/c")
def track_by_number(
    request: Request,
    db: DB,
    o: int = Query(ge=1),
    a: int = Query(ge=1),
    sub_id: str | None = Query(None, max_length=100),
):
    link = db.scalar(
        select(TrackingLink)
        .join(Offer, Offer.id == TrackingLink.offer_id)
        .join(User, User.id == TrackingLink.affiliate_id)
        .where(Offer.public_number == o, User.public_number == a)
    )
    if not link:
        raise HTTPException(404, "Tracking link not found")
    return track(link.id, request, db, sub_id)


@router.get("/t/{link_id}")
def track(link_id: str, request: Request, db: DB, sub_id: str | None = Query(None, max_length=100)):
    from app.features.campaigns.policy import policy
    from app.features.tracking.runtime import record_click

    try:
        link, offer, click, target = record_click(db, request, link_id, sub_id)
        db.commit()
    except HTTPException as error:
        # Only documented availability failures may use the fixed fallback.
        # Fraud, authorization and publisher approval failures never bypass checks.
        allowed = {
            "Traffic does not match offer targeting",
            "Offer is outside its schedule",
            "Offer cap reached",
            "Campaign period cap reached",
        }
        if error.detail not in allowed:
            raise
        db.rollback()
        link = db.get(TrackingLink, link_id)
        offer = db.get(Offer, link.offer_id) if link else None
        fallback = policy(offer).fallback_url if offer else None
        if not fallback:
            raise
        return RedirectResponse(str(fallback), status_code=302)
    return RedirectResponse(target, status_code=302)
