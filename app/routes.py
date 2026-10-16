import secrets
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select, func, update
from sqlalchemy.exc import IntegrityError
from .config import get_settings
from .models import Workspace, User, Offer, TrackingLink, Click, Conversion, AuditEvent
from .schemas import UserCreate, OfferCreate, LinkCreate, Postback, StatusUpdate, ActiveUpdate
from .security import DB, Actor, passwords, staff_only, key_hash
from .services import audit, quota, entitlement, utc, enqueue
from .auth import issue_action
from .models import WebhookEndpoint, AuthSession, CommissionEntry

settings = get_settings()
router = APIRouter()


def output_user(user):
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "active": user.active,
        "parent_user_id": user.parent_user_id,
        "email_verified": user.email_verified,
        "name": user.full_name,
    }


def output_offer(offer):
    from .tenancy import company_context

    company = company_context.get()
    result = {
        key: getattr(offer, key)
        for key in (
            "id",
            "name",
            "destination_url",
            "revenue",
            "payout",
            "currency",
            "active",
            "advertiser_id",
            "attribution_hours",
        )
    }
    result["revenue"] = str(offer.revenue)
    result["payout"] = str(offer.payout)
    prefix = "/c/" + company.slug if company else ""
    result["postback_url"] = (
        settings.public_base_url.rstrip("/") + prefix + "/postbacks/" + offer.id
    )
    return result


def entitled(db, user):
    entitlement(db.get(Workspace, user.workspace_id))


def visible_users(user):
    query = select(User.id).where(User.workspace_id == user.workspace_id)
    if user.role == "affiliate":
        query = query.where((User.id == user.id) | (User.parent_user_id == user.id))
    elif user.role == "publisher":
        query = query.where(User.id == user.id)
    else:
        raise HTTPException(403, "Affiliate or publisher required")
    return query


def owned(db, model, object_id, user):
    result = db.scalar(
        select(model).where(model.id == object_id, model.workspace_id == user.workspace_id)
    )
    if not result:
        raise HTTPException(404, "Resource not found")
    return result


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/me")
def me(user: Actor):
    return output_user(user)


@router.post("/users", status_code=201)
def create_user(body: UserCreate, db: DB, user: Actor):
    if user.role not in {"admin", "affiliate"}:
        raise HTTPException(403, "Admin or affiliate access required")
    parent_id = body.parent_user_id
    if user.role == "affiliate":
        if body.role != "publisher" or (parent_id and parent_id != user.id):
            raise HTTPException(403, "Affiliates can create only their own publishers")
        parent_id = user.id
    if body.role == "publisher":
        if not parent_id or owned(db, User, parent_id, user).role != "affiliate":
            raise HTTPException(400, "Publisher requires an affiliate parent")
    elif parent_id:
        raise HTTPException(400, "Only publishers can have a parent")
    quota(db, user, User, "users")
    member = User(
        workspace_id=user.workspace_id,
        email=str(body.email).lower(),
        password_hash=passwords.hash(body.password),
        role=body.role,
        parent_user_id=parent_id,
        email_verified=not settings.require_email_verification,
    )
    db.add(member)
    try:
        db.flush()
        audit(db, user, "user.created", member.id, member.role)
        if settings.require_email_verification:
            issue_action(db, member, "verify")
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Email is already registered")
    return output_user(member)


@router.get("/users")
def list_users(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    entitled(db, user)
    if user.role not in {"admin", "staff", "affiliate"}:
        raise HTTPException(403, "User management access required")
    query = select(User).where(User.workspace_id == user.workspace_id)
    if user.role == "affiliate":
        query = query.where(User.parent_user_id == user.id)
    return [output_user(u) for u in db.scalars(query.order_by(User.id).offset(offset).limit(limit))]


@router.patch("/users/{user_id}")
def set_user_active(user_id: str, body: ActiveUpdate, db: DB, user: Actor):
    entitled(db, user)
    if user.role not in {"admin", "affiliate"}:
        raise HTTPException(403, "Admin or affiliate access required")
    if user_id == user.id:
        raise HTTPException(400, "Cannot deactivate your own account")
    member = owned(db, User, user_id, user)
    if user.role == "affiliate" and (
        member.role != "publisher" or member.parent_user_id != user.id
    ):
        raise HTTPException(404, "Publisher not found")
    member.active = body.active
    member.auth_version += 1
    db.execute(update(AuthSession).where(AuthSession.user_id == member.id).values(revoked=True))
    audit(db, user, "user.active", member.id, str(body.active))
    db.commit()
    return {**output_user(member), "active": member.active}


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


@router.post("/links", status_code=201)
def create_link(
    body: LinkCreate,
    db: DB,
    user: Actor,
    tracking_domain_id: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
):
    entitled(db, user)
    from .domains import link_base

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
    return {"id": link.id, "url": f"{base}/t/{link.id}"}


@router.get("/links")
def list_links(
    db: DB,
    user: Actor,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    tracking_domain_id: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
):
    entitled(db, user)
    from .domains import link_base

    base = link_base(db, user, tracking_domain_id)
    if user.role not in {"admin", "staff", "affiliate", "publisher"}:
        raise HTTPException(403, "Tracking link access required")
    query = select(TrackingLink).where(TrackingLink.workspace_id == user.workspace_id)
    if user.role in {"affiliate", "publisher"}:
        query = query.where(TrackingLink.affiliate_id.in_(visible_users(user)))
    return [
        {
            "id": link.id,
            "offer_id": link.offer_id,
            "affiliate_id": link.affiliate_id,
            "url": f"{base}/t/{link.id}",
        }
        for link in db.scalars(query.order_by(TrackingLink.id).offset(offset).limit(limit))
    ]


@router.get("/t/{link_id}")
def track(link_id: str, request: Request, db: DB, sub_id: str | None = Query(None, max_length=100)):
    link = db.get(TrackingLink, link_id)
    if not link:
        raise HTTPException(404, "Tracking link not found")
    offer = db.get(Offer, link.offer_id)
    affiliate = db.get(User, link.affiliate_id)
    entitlement(db.get(Workspace, link.workspace_id))
    if affiliate.parent_user_id and not db.get(User, affiliate.parent_user_id).active:
        raise HTTPException(410, "Parent affiliate disabled")
    if not offer.active or not affiliate.active:
        raise HTTPException(410, "Tracking link disabled")
    from .tracking import check_click, reserve
    from .domains import enforce_tracking_host

    enforce_tracking_host(db, request, link.workspace_id)
    metadata = check_click(db, offer, request)
    at = datetime.now(timezone.utc)
    reserve(db, offer, "clicks", at)
    click = Click(
        workspace_id=link.workspace_id, link_id=link.id, sub_id=sub_id, created_at=at, **metadata
    )
    db.add(click)
    db.commit()
    parts = urlsplit(offer.destination_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "click_id"]
    query.append(("click_id", click.id))
    return RedirectResponse(urlunsplit(parts._replace(query=urlencode(query))), status_code=302)


@router.post("/postbacks/{offer_id}", status_code=201)
def postback(
    offer_id: str, body: Postback, db: DB, x_postback_key: Annotated[str, Header(max_length=256)]
):
    offer = db.get(Offer, offer_id)
    if not offer or not secrets.compare_digest(offer.postback_key_hash, key_hash(x_postback_key)):
        raise HTTPException(401, "Invalid postback credentials")
    click = db.scalar(
        select(Click)
        .join(TrackingLink, Click.link_id == TrackingLink.id)
        .where(
            Click.id == body.click_id,
            Click.workspace_id == offer.workspace_id,
            TrackingLink.offer_id == offer.id,
        )
        .with_for_update(of=Click)
    )
    if not click:
        raise HTTPException(404, "Click not found for this offer")
    # Retries remain idempotent even after the attribution window or subscription expires.
    existing = db.scalar(
        select(Conversion).where(
            Conversion.workspace_id == offer.workspace_id,
            Conversion.offer_id == offer.id,
            Conversion.transaction_id == body.transaction_id,
        )
    )
    if existing and existing.click_id == body.click_id:
        return JSONResponse(
            {"id": existing.id, "status": existing.status, "duplicate": True}, status_code=200
        )
    entitlement(db.get(Workspace, offer.workspace_id))
    if utc(click.created_at) < datetime.now(timezone.utc) - timedelta(
        hours=offer.attribution_hours
    ):
        raise HTTPException(422, "Click attribution window expired")
    from .tracking import Rules, reject_fraud, reserve

    rules = Rules.model_validate(offer.tracking_rules)
    if (
        datetime.now(timezone.utc) - utc(click.created_at)
    ).total_seconds() < rules.minimum_conversion_seconds:
        reject_fraud(db, offer, "conversion_too_fast", 80, click.ip_hash, click.id)
    at = datetime.now(timezone.utc)
    reserve(db, offer, "conversions", at)
    conversion = Conversion(
        workspace_id=offer.workspace_id,
        offer_id=offer.id,
        **body.model_dump(),
        revenue=offer.revenue,
        payout=offer.payout,
        currency=offer.currency,
        created_at=at,
    )
    db.add(conversion)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(Conversion).where(
                Conversion.workspace_id == offer.workspace_id,
                Conversion.offer_id == offer.id,
                Conversion.transaction_id == body.transaction_id,
            )
        )
        if existing and existing.click_id == body.click_id:
            return JSONResponse(
                {"id": existing.id, "status": existing.status, "duplicate": True}, status_code=200
            )
        raise HTTPException(409, "Transaction or click already converted")
    return {"id": conversion.id, "status": conversion.status, "duplicate": False}


@router.patch("/conversions/{conversion_id}")
def update_conversion(conversion_id: str, body: StatusUpdate, db: DB, user: Actor):
    entitled(db, user)
    staff_only(user)
    conversion = db.scalar(
        select(Conversion)
        .where(Conversion.id == conversion_id, Conversion.workspace_id == user.workspace_id)
        .with_for_update()
    )
    if not conversion:
        raise HTTPException(404, "Resource not found")
    if conversion.status == body.status:
        return {"id": conversion.id, "status": conversion.status}
    previous = conversion.status
    audit(db, user, "conversion.status", conversion.id, f"{previous} -> {body.status}")
    conversion.status = body.status
    conversion.version += 1
    link = db.scalar(
        select(TrackingLink)
        .join(Click, Click.link_id == TrackingLink.id)
        .where(Click.id == conversion.click_id)
    )
    # Lock the commission account shared with payout recording.
    db.scalar(select(User).where(User.id == link.affiliate_id).with_for_update())
    change = (
        conversion.payout
        if body.status == "approved"
        else -conversion.payout
        if previous == "approved"
        else None
    )
    if change is not None:
        db.add(
            CommissionEntry(
                workspace_id=user.workspace_id,
                affiliate_id=link.affiliate_id,
                conversion_id=conversion.id,
                version=conversion.version,
                amount=change,
                currency=conversion.currency,
            )
        )
    endpoint = db.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.workspace_id == user.workspace_id,
            WebhookEndpoint.affiliate_id == link.affiliate_id,
            WebhookEndpoint.active.is_(True),
        )
    )
    if endpoint:
        enqueue(
            db,
            user.workspace_id,
            "webhook",
            {
                "endpoint_id": endpoint.id,
                "event": {
                    "conversion_id": conversion.id,
                    "click_id": conversion.click_id,
                    "transaction_id": conversion.transaction_id,
                    "status": conversion.status,
                    "payout": str(conversion.payout),
                    "currency": conversion.currency,
                    "version": conversion.version,
                },
            },
        )
    db.commit()
    return {"id": conversion.id, "status": conversion.status}


@router.get("/conversions")
def list_conversions(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    entitled(db, user)
    query = select(Conversion).where(Conversion.workspace_id == user.workspace_id)
    if user.role in {"affiliate", "publisher"}:
        query = (
            query.join(Click, Conversion.click_id == Click.id)
            .join(TrackingLink, Click.link_id == TrackingLink.id)
            .where(TrackingLink.affiliate_id.in_(visible_users(user)))
        )
    elif user.role == "advertiser":
        query = query.join(Offer, Conversion.offer_id == Offer.id).where(
            Offer.advertiser_id == user.id
        )
    else:
        staff_only(user)
    rows = [
        {
            k: getattr(row, k)
            for k in (
                "id",
                "click_id",
                "offer_id",
                "transaction_id",
                "status",
                "revenue",
                "payout",
                "currency",
                "created_at",
            )
        }
        for row in db.scalars(
            query.order_by(Conversion.created_at.desc(), Conversion.id).offset(offset).limit(limit)
        )
    ]
    for row in rows:
        row["revenue"] = str(row["revenue"])
        row["payout"] = str(row["payout"])
    if user.role in {"affiliate", "publisher"}:
        for row in rows:
            row.pop("revenue")
    elif user.role == "advertiser":
        for row in rows:
            row.pop("payout")
    return rows


@router.get("/audit-events")
def list_audit(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    if user.role not in {"admin", "platform_admin"}:
        raise HTTPException(403, "Admin access required")
    query = (
        select(AuditEvent)
        .where(AuditEvent.workspace_id == user.workspace_id)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
        .offset(offset)
        .limit(limit)
    )
    return [
        {
            k: getattr(row, k)
            for k in ("id", "actor_id", "action", "resource_id", "detail", "created_at")
        }
        for row in db.scalars(query)
    ]


@router.get("/reports/summary")
def summary(db: DB, user: Actor, start: datetime | None = None, end: datetime | None = None):
    entitled(db, user)
    if user.role not in {"admin", "staff", "affiliate", "publisher", "advertiser"}:
        raise HTTPException(403, "Reporting access required")
    if (start and start.tzinfo is None) or (end and end.tzinfo is None):
        raise HTTPException(422, "Dates must include a timezone")
    if start and end and start >= end:
        raise HTTPException(422, "Start must precede end")
    clicks = select(func.count(Click.id)).where(Click.workspace_id == user.workspace_id)
    conversions = select(
        Conversion.currency,
        func.count(Conversion.id),
        func.coalesce(func.sum(Conversion.revenue), 0),
        func.coalesce(func.sum(Conversion.payout), 0),
    ).where(Conversion.workspace_id == user.workspace_id, Conversion.status == "approved")
    if user.role in {"affiliate", "publisher"}:
        clicks = clicks.join(TrackingLink, Click.link_id == TrackingLink.id).where(
            TrackingLink.affiliate_id.in_(visible_users(user))
        )
        conversions = (
            conversions.join(Click, Conversion.click_id == Click.id)
            .join(TrackingLink, Click.link_id == TrackingLink.id)
            .where(TrackingLink.affiliate_id.in_(visible_users(user)))
        )
    elif user.role == "advertiser":
        clicks = (
            clicks.join(TrackingLink, Click.link_id == TrackingLink.id)
            .join(Offer, TrackingLink.offer_id == Offer.id)
            .where(Offer.advertiser_id == user.id)
        )
        conversions = conversions.join(Offer, Conversion.offer_id == Offer.id).where(
            Offer.advertiser_id == user.id
        )
    for boundary, operator in ((start, "__ge__"), (end, "__lt__")):
        if boundary:
            clicks = clicks.where(getattr(Click.created_at, operator)(boundary))
            conversions = conversions.where(getattr(Conversion.created_at, operator)(boundary))
    totals = []
    for currency, count, revenue, payout in db.execute(conversions.group_by(Conversion.currency)):
        item = {"currency": currency, "approved_conversions": count, "payout": str(payout)}
        if user.role == "advertiser":
            item.pop("payout")
            item["revenue"] = str(revenue)
        elif user.role not in {"affiliate", "publisher"}:
            item.update(revenue=str(revenue), profit=str(Decimal(revenue) - Decimal(payout)))
        totals.append(item)
    return {"clicks": db.scalar(clicks), "totals": totals}
