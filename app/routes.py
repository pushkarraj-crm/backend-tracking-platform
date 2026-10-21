import secrets
from datetime import datetime
from decimal import Decimal
from typing import Annotated
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select, func, update
from sqlalchemy.exc import IntegrityError
from .config import get_settings
from .models import Workspace, User, Offer, TrackingLink, Click, Conversion, AuditEvent
from .schemas import UserCreate, OfferCreate, LinkCreate, Postback, StatusUpdate, ActiveUpdate
from .security import DB, Actor, passwords, staff_only, key_hash
from .services import audit, quota, entitlement
from .auth import issue_action
from .models import AuthSession

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
        "api_enabled": user.api_enabled,
        "staff_permissions": user.staff_permissions,
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
    query = select(model).where(model.id == object_id, model.workspace_id == user.workspace_id)
    if model is User:
        # Platform identities are not tenant-managed partners, even when an old
        # owner activation retained the account's original workspace reference.
        query = query.where(User.role != "platform_admin")
    result = db.scalar(query)
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
        if parent_id and owned(db, User, parent_id, user).role != "affiliate":
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
    query = select(User).where(
        User.workspace_id == user.workspace_id, User.role != "platform_admin"
    )
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
    from .tracking_runtime import record_click
    from .campaign_policy import policy

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


@router.post("/postbacks/{offer_id}", status_code=201)
def postback(
    offer_id: str,
    body: Postback,
    request: Request,
    db: DB,
    x_postback_key: Annotated[str, Header(max_length=256)],
):
    from .conversion_engine import ingest, existing_conversion, same_payload
    from .campaign_policy import policy
    from .network_models import PostbackReceipt
    import ipaddress

    offer = db.get(Offer, offer_id)
    if not offer or not secrets.compare_digest(offer.postback_key_hash, key_hash(x_postback_key)):
        raise HTTPException(401, "Invalid postback credentials")
    workspace_id = offer.workspace_id

    def receipt(result, status):
        db.add(
            PostbackReceipt(
                workspace_id=workspace_id,
                offer_id=offer_id,
                click_id=body.click_id,
                transaction_id=body.transaction_id,
                event_key=body.event_key,
                result=result,
                http_status=status,
            )
        )

    try:
        ranges = policy(offer).postback_ip_ranges
        if ranges:
            try:
                address = ipaddress.ip_address(request.client.host if request.client else "")
                permitted = any(address in ipaddress.ip_network(v) for v in ranges)
            except ValueError:
                permitted = False
            if not permitted:
                raise HTTPException(403, "Advertiser postback IP is not permitted")
        try:
            conversion, duplicate = ingest(db, offer, body)
            db.flush()
        except IntegrityError:
            db.rollback()
            offer = db.get(Offer, offer_id)
            conversion = existing_conversion(db, offer, body)
            if not conversion or not same_payload(conversion, body):
                raise HTTPException(409, "Transaction or click already converted") from None
            duplicate = True
        result = {"id": conversion.id, "status": conversion.status, "duplicate": duplicate}
        receipt("duplicate" if duplicate else "accepted", 200 if duplicate else 201)
        db.commit()
    except HTTPException as error:
        db.rollback()
        reason = {
            403: "policy_rejected",
            404: "click_not_found",
            409: "conflict",
            422: "invalid_event_or_payload",
            429: "cap_reached",
            402: "subscription_required",
        }.get(error.status_code, "rejected")
        receipt(reason, error.status_code)
        db.commit()
        raise
    return JSONResponse(result, status_code=200) if duplicate else result


@router.patch("/conversions/{conversion_id}")
def update_conversion(conversion_id: str, body: StatusUpdate, db: DB, user: Actor):
    entitled(db, user)
    conversion = db.scalar(
        select(Conversion)
        .where(Conversion.id == conversion_id, Conversion.workspace_id == user.workspace_id)
        .with_for_update()
    )
    if not conversion:
        raise HTTPException(404, "Resource not found")
    if user.role == "advertiser":
        offer = db.get(Offer, conversion.offer_id)
        if offer.advertiser_id != user.id:
            raise HTTPException(404, "Resource not found")
    else:
        staff_only(user)
    from .conversion_engine import transition

    transition(db, user, conversion, body.status)
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
                "event_key",
                "event_type",
                "sale_amount",
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
        if row["sale_amount"] is not None:
            row["sale_amount"] = str(row["sale_amount"])
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
    from .network_models import Impression
    from .reporting import scope, Filters

    report_filters = Filters()
    impression_query = scope(
        select(func.count(Impression.id))
        .join(TrackingLink, TrackingLink.id == Impression.link_id)
        .join(Offer, Offer.id == TrackingLink.offer_id),
        user,
        report_filters,
        Impression,
    )
    unique_query = scope(
        select(func.count(func.distinct(Conversion.click_id)))
        .join(Click, Click.id == Conversion.click_id)
        .join(TrackingLink, TrackingLink.id == Click.link_id)
        .join(Offer, Offer.id == TrackingLink.offer_id)
        .where(Conversion.status == "approved"),
        user,
        report_filters,
    )
    for boundary, operator in ((start, "__ge__"), (end, "__lt__")):
        if boundary:
            impression_query = impression_query.where(
                getattr(Impression.created_at, operator)(boundary)
            )
            unique_query = unique_query.where(getattr(Conversion.created_at, operator)(boundary))
    return {
        "clicks": db.scalar(clicks),
        "impressions": db.scalar(impression_query),
        "approved_unique_clicks": db.scalar(unique_query),
        "totals": totals,
    }
