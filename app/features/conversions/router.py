import secrets
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.security import DB, Actor, key_hash
from app.core.services import audit
from app.permissions.resources import entitled, visible_users
from app.permissions.roles import staff_only
from app.persistence.models import Click, Conversion, Offer, TrackingLink, Workspace
from app.shared.schemas import Postback, StatusUpdate

router = APIRouter()


@router.post("/postbacks/{offer_id}", status_code=201)
def postback(
    offer_id: str,
    body: Postback,
    request: Request,
    db: DB,
    x_postback_key: Annotated[str, Header(max_length=256)],
):
    offer = db.get(Offer, offer_id)
    if not offer or not secrets.compare_digest(offer.postback_key_hash, key_hash(x_postback_key)):
        raise HTTPException(401, "Invalid postback credentials")
    return accept_postback(db, offer, body, request)


def accept_postback(db, offer, body, request, status=None, offline=False, browser=False):
    """Shared by per-campaign and global postbacks: policy, ingest, receipts, status updates."""
    import ipaddress

    from app.features.campaigns.policy import policy
    from app.features.conversions.service import (
        approve_lead,
        campaign_actor,
        existing_conversion,
        ingest,
        same_payload,
        transition,
    )
    from app.persistence.network_models import PostbackReceipt

    offer_id, workspace_id = offer.id, offer.workspace_id

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
            conversion, duplicate = ingest(db, offer, body, offline=offline, browser=browser)
            db.flush()
        except IntegrityError:
            db.rollback()
            offer = db.get(Offer, offer_id)
            conversion = existing_conversion(db, offer, body)
            if not conversion or not same_payload(conversion, body):
                raise HTTPException(409, "Transaction or click already converted") from None
            duplicate = True
        if status and conversion.status != status:
            # Advertisers may report a later status for the same transaction.
            actor = campaign_actor(db, offer)
            transition(db, actor, conversion, status)
            if status == "approved":
                approve_lead(db, actor, offer, conversion.transaction_id, conversion.event_key)
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


GLOBAL_EVENTS = {"", "initial", "lead", "sale", "conversion"}


@router.api_route("/pb/{token}", methods=["GET", "POST"], status_code=201)
async def global_postback(token: str, request: Request, db: DB):
    """One postback URL per account; the click identifies the campaign."""
    from starlette.concurrency import run_in_threadpool

    values = dict(request.query_params)
    if request.method == "POST":
        try:
            data = (
                await request.json()
                if "json" in request.headers.get("content-type", "")
                else dict(await request.form())
            )
        except ValueError:
            raise HTTPException(422, "Postback body must be JSON or form data") from None
        if not isinstance(data, dict):
            raise HTTPException(422, "Postback body must be an object")
        values.update(data)
    values = {str(k).lower(): str(v).strip() for k, v in values.items() if v is not None}
    return await run_in_threadpool(receive_global_postback, db, token, values, request)


def receive_global_postback(db, token, values, request):
    workspace = (
        db.scalar(select(Workspace).where(Workspace.postback_token_hash == key_hash(token)))
        if 16 <= len(token) <= 128
        else None
    )
    if not workspace:
        raise HTTPException(401, "Invalid postback credentials")
    return record_values(db, workspace, values, request)


def record_values(db, workspace, values, request, browser=False):
    """Shared by the server postback and the browser endpoint: click or coupon, then ingest."""
    from pydantic import ValidationError

    from app.features.coupons.router import coupon_click, usable_coupon
    from app.persistence.network_models import CampaignEvent

    click = db.scalar(
        select(Click).where(
            Click.id == values.get("click_id", ""), Click.workspace_id == workspace.id
        )
    )
    event = values.get("event", "").lower()
    event_key = "initial" if event in GLOBAL_EVENTS else event
    if values.get("coupon"):
        values["coupon"] = values["coupon"].upper()  # codes are case-insensitive
    coupon = usable_coupon(db, workspace.id, values["coupon"]) if values.get("coupon") else None
    link = db.get(TrackingLink, click.link_id) if click else None
    # A partner's coupon code wins over a click from someone else (or no click at all).
    by_coupon = bool(coupon) and (
        not link or (link.offer_id, link.affiliate_id) != (coupon.offer_id, coupon.partner_id)
    )
    if by_coupon:
        if not values.get("txn_id"):
            raise HTTPException(422, "txn_id is required when a sale is credited by coupon")
        earlier = db.scalar(
            select(Conversion).where(
                Conversion.workspace_id == workspace.id,
                Conversion.offer_id == coupon.offer_id,
                Conversion.event_key == event_key,
                Conversion.transaction_id == values["txn_id"],
            )
        )
        click = db.get(Click, earlier.click_id) if earlier else coupon_click(db, coupon)
    if not click:
        raise HTTPException(
            404,
            "Click not found" if values.get("click_id") else "Send a click_id or a valid coupon",
        )
    offer = db.get(Offer, db.get(TrackingLink, click.link_id).offer_id)
    model = offer.campaign_model
    if event_key != "initial":
        model = db.scalar(
            select(CampaignEvent.model).where(
                CampaignEvent.workspace_id == offer.workspace_id,
                CampaignEvent.offer_id == offer.id,
                CampaignEvent.code == event_key,
            )
        )
    # Other platforms often send an amount on every event; it only prices CPS events.
    amount = values.get("amount") if model == "CPS" else None
    # Browsers cannot set a status; their sales stay pending until confirmed.
    status = None if browser else values.get("status", "").lower() or None
    if status not in {None, "approved", "rejected", "pending"}:
        raise HTTPException(422, "status must be approved, rejected or pending")
    try:
        body = Postback(
            click_id=click.id,
            transaction_id=values.get("txn_id") or click.id,
            event_key=event_key,
            event_type="sale" if amount else ("lead" if event == "lead" else None),
            sale_amount=amount or None,
            sale_currency=(values.get("currency", "").upper() or None) if amount else None,
            advertiser_parameters={
                k: values[k]
                for k in (*[f"adv_sub{i}" for i in range(1, 6)], "coupon")
                if values.get(k)
            },
        )
    except ValidationError as error:
        raise HTTPException(422, "; ".join(e["msg"] for e in error.errors())) from None
    result = accept_postback(db, offer, body, request, status, offline=by_coupon, browser=browser)
    if by_coupon and isinstance(result, dict):  # a new sale, not a repeated postback
        from sqlalchemy import update

        from app.persistence.network_models import Coupon

        # Atomic increment; the quota was checked under a row lock before the sale was recorded.
        db.execute(update(Coupon).where(Coupon.id == coupon.id).values(uses=Coupon.uses + 1))
        db.commit()
    return result


def account_postback_url(db, workspace):
    """The account's global postback URL, creating its secret on first use."""
    from app.core.services import decrypt
    from app.tenancy.service import company_context

    if workspace.postback_token:
        token = decrypt(workspace.postback_token)
    else:
        token = rotate_postback_token(db, workspace)
    company = company_context.get()
    prefix = "/c/" + company.slug if company else ""
    return get_settings().public_base_url.rstrip("/") + prefix + "/pb/" + token


def rotate_postback_token(db, workspace):
    from app.core.services import encrypt

    token = secrets.token_urlsafe(24)
    workspace.postback_token_hash = key_hash(token)
    workspace.postback_token = encrypt(token)
    db.commit()
    return token


@router.get("/postback-settings")
def postback_settings(db: DB, user: Actor):
    staff_only(user)
    return {"url": account_postback_url(db, db.get(Workspace, user.workspace_id))}


@router.post("/postback-settings/rotate")
def rotate_postback(db: DB, user: Actor):
    if user.role != "admin":
        raise HTTPException(403, "Network admin required")
    workspace = db.get(Workspace, user.workspace_id)
    rotate_postback_token(db, workspace)
    audit(db, user, "postback.rotated", workspace.id, "global postback secret")
    db.commit()
    return {"url": account_postback_url(db, workspace)}


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
    from app.features.conversions.service import transition

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
