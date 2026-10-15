import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
import httpx
from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import Field
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from .models import (
    OutboxEvent,
    PayoutProvider,
    PayoutRequest,
    PayoutBeneficiary,
    PayoutAdjustment,
    Payout,
    User,
    Workspace,
    BillingEvent,
    now,
)
from .schemas import StrictModel
from .security import DB, Actor
from .services import encrypt, decrypt, enqueue, audit, entitlement
from .config import get_settings
from .payouts import entries
from .routes import owned, visible_users

router = APIRouter(prefix="/automated-payouts", tags=["Automated payouts"])
OPEN = {"queued", "submitting", "processing"}


class ProviderBody(StrictModel):
    key_id: str = Field(pattern=r"^rzp_(test|live)_[A-Za-z0-9]+$", max_length=100)
    key_secret: str = Field(min_length=10, max_length=200)
    account_number: str = Field(min_length=6, max_length=40, pattern=r"^[a-zA-Z0-9]+$")
    webhook_secret: str = Field(min_length=32, max_length=200)
    automatic: bool = False
    minimum_amount: Decimal = Field(default=1000, ge=1, max_digits=14, decimal_places=2)
    interval_hours: int = Field(default=168, ge=1, le=744)


class BeneficiaryBody(StrictModel):
    affiliate_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    fund_account_id: str = Field(pattern=r"^fa_[a-zA-Z0-9]+$", max_length=100)


class RequestBody(StrictModel):
    affiliate_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    request_key: str = Field(min_length=16, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")


def admin(user):
    if user.role != "admin":
        raise HTTPException(403, "Workspace admin required")


def provider_call(credentials, method, path, body=None, idempotency=None):
    headers = {"X-Payout-Idempotency": idempotency} if idempotency else {}
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        response = client.request(
            method,
            "https://api.razorpay.com/v1/" + path,
            json=body,
            auth=(credentials["key_id"], credentials["key_secret"]),
            headers=headers,
        )
    if response.status_code >= 400:
        raise RuntimeError("Payout provider rejected request")
    return response.json()


@router.put("/provider")
def configure(body: ProviderBody, db: DB, user: Actor):
    admin(user)
    entitlement(db.get(Workspace, user.workspace_id))
    # Serialize against payout creation and block credential changes while money is reserved.
    db.scalar(select(Workspace).where(Workspace.id == user.workspace_id).with_for_update())
    provider = db.scalar(
        select(PayoutProvider).where(PayoutProvider.workspace_id == user.workspace_id)
    )
    outstanding = db.scalar(
        select(PayoutRequest)
        .where(PayoutRequest.workspace_id == user.workspace_id, PayoutRequest.status.in_(OPEN))
        .limit(1)
    )
    if outstanding:
        old = json.loads(decrypt(provider.credentials))
        if old["account_number"] != body.account_number:
            raise HTTPException(409, "Cannot change funding account while payouts are outstanding")
        if old["key_id"] != body.key_id:
            credentials = body.model_dump(mode="json")
            try:
                fund = provider_call(
                    credentials, "GET", "fund_accounts/" + outstanding.fund_account_id
                )
            except Exception:
                raise HTTPException(
                    409, "New credentials must verify the existing merchant fund account"
                )
            if fund.get("id") != outstanding.fund_account_id:
                raise HTTPException(409, "Merchant identity could not be verified")
    if not provider:
        provider = PayoutProvider(workspace_id=user.workspace_id)
        db.add(provider)
    provider.credentials = encrypt(
        json.dumps(
            body.model_dump(
                mode="json", include={"key_id", "key_secret", "account_number", "webhook_secret"}
            )
        )
    )
    provider.automatic = body.automatic
    provider.minimum_amount = body.minimum_amount
    provider.interval_hours = body.interval_hours
    provider.next_run_at = now() + timedelta(hours=body.interval_hours)
    provider.configured_by = user.id
    db.flush()
    audit(db, user, "payout.provider_configured", provider.id)
    db.commit()
    return {
        "configured": True,
        "automatic": provider.automatic,
        "webhook_url": get_settings().public_base_url.rstrip("/")
        + "/automated-payouts/webhooks/"
        + provider.id,
    }


@router.get("/provider")
def provider_info(db: DB, user: Actor):
    admin(user)
    provider = db.scalar(
        select(PayoutProvider).where(PayoutProvider.workspace_id == user.workspace_id)
    )
    if not provider:
        return {"configured": False}
    return {
        "configured": True,
        "automatic": provider.automatic,
        "minimum_amount": str(provider.minimum_amount),
        "currency": "INR",
        "interval_hours": provider.interval_hours,
        "next_run_at": provider.next_run_at,
    }


@router.put("/beneficiaries")
def beneficiary(body: BeneficiaryBody, db: DB, user: Actor):
    admin(user)
    affiliate = owned(db, User, body.affiliate_id, user)
    if affiliate.role not in {"affiliate", "publisher"}:
        raise HTTPException(422, "Affiliate or publisher required")
    provider = db.scalar(
        select(PayoutProvider).where(PayoutProvider.workspace_id == user.workspace_id)
    )
    if not provider:
        raise HTTPException(409, "Configure the network payout provider first")
    try:
        account = provider_call(
            json.loads(decrypt(provider.credentials)),
            "GET",
            "fund_accounts/" + body.fund_account_id,
        )
    except Exception:
        raise HTTPException(502, "Could not verify fund account with this network provider")
    if account.get("id") != body.fund_account_id:
        raise HTTPException(422, "Invalid fund account")
    record = db.scalar(
        select(PayoutBeneficiary).where(
            PayoutBeneficiary.workspace_id == user.workspace_id,
            PayoutBeneficiary.affiliate_id == affiliate.id,
        )
    )
    if not record:
        record = PayoutBeneficiary(workspace_id=user.workspace_id, affiliate_id=affiliate.id)
        db.add(record)
    record.fund_account_id = body.fund_account_id
    db.flush()
    audit(db, user, "payout.beneficiary", record.id)
    db.commit()
    return {"verified": True}


def available(db, workspace_id, affiliate_id):
    # A staff-shaped actor is used only by the scheduler after selecting this exact network.
    actor = User(workspace_id=workspace_id, role="admin")
    records = entries(actor)
    return db.scalar(
        select(func.coalesce(func.sum(records.c.amount), 0)).where(
            records.c.affiliate_id == affiliate_id, records.c.currency == "INR"
        )
    )


def queue_request(db, workspace_id, affiliate_id, amount, key, actor_id=None):
    db.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update())
    existing = db.scalar(
        select(PayoutRequest).where(
            PayoutRequest.workspace_id == workspace_id, PayoutRequest.request_key == key
        )
    )
    if existing:
        if existing.affiliate_id != affiliate_id or existing.amount != amount:
            raise HTTPException(409, "Idempotency key has different payout parameters")
        return existing
    provider = db.scalar(select(PayoutProvider).where(PayoutProvider.workspace_id == workspace_id))
    if not provider:
        raise HTTPException(409, "Network payout provider is not configured")
    entitlement(db.get(Workspace, workspace_id))
    affiliate = db.scalar(
        select(User)
        .where(User.id == affiliate_id, User.workspace_id == workspace_id)
        .with_for_update()
    )
    if not affiliate or not affiliate.active or affiliate.role not in {"affiliate", "publisher"}:
        raise HTTPException(404, "Recipient not found")
    beneficiary = db.scalar(
        select(PayoutBeneficiary).where(
            PayoutBeneficiary.workspace_id == workspace_id,
            PayoutBeneficiary.affiliate_id == affiliate_id,
        )
    )
    if not beneficiary:
        raise HTTPException(409, "Recipient has no verified fund account")
    if amount > available(db, workspace_id, affiliate_id):
        raise HTTPException(409, "Payout exceeds available INR commissions")
    record = PayoutRequest(
        workspace_id=workspace_id,
        affiliate_id=affiliate_id,
        amount=amount,
        currency="INR",
        request_key=key,
        fund_account_id=beneficiary.fund_account_id,
        requested_by=actor_id or provider.configured_by,
    )
    db.add(record)
    db.flush()
    event = enqueue(db, workspace_id, "payout", {"request_id": record.id})
    db.flush()
    record.outbox_id = event.id
    return record


@router.post("/requests", status_code=202)
def request_payout(body: RequestBody, db: DB, user: Actor):
    admin(user)
    record = queue_request(
        db, user.workspace_id, body.affiliate_id, body.amount, body.request_key, actor_id=user.id
    )
    audit(db, user, "payout.requested", record.id)
    db.commit()
    return {
        "id": record.id,
        "status": record.status,
        "amount": str(record.amount),
        "currency": record.currency,
    }


@router.get("/requests")
def list_requests(db: DB, user: Actor, limit: int = Query(50, ge=1, le=100)):
    query = select(PayoutRequest).where(PayoutRequest.workspace_id == user.workspace_id)
    if user.role in {"affiliate", "publisher"}:
        query = query.where(PayoutRequest.affiliate_id.in_(visible_users(user)))
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Payout access required")
    return [
        {
            "id": p.id,
            "affiliate_id": p.affiliate_id,
            "amount": str(p.amount),
            "currency": p.currency,
            "status": p.status,
            "created_at": p.created_at,
        }
        for p in db.scalars(query.order_by(PayoutRequest.created_at.desc()).limit(limit))
    ]


def process(db, request_id, workspace_id):
    record = db.scalar(
        select(PayoutRequest)
        .where(PayoutRequest.id == request_id, PayoutRequest.workspace_id == workspace_id)
        .with_for_update()
    )
    if not record or record.status in {"failed", "cancelled", "reversed"}:
        return
    provider = db.scalar(select(PayoutProvider).where(PayoutProvider.workspace_id == workspace_id))
    credentials = json.loads(decrypt(provider.credentials))
    if record.provider_id:
        state = provider_call(credentials, "GET", "payouts/" + record.provider_id)
    else:
        state = provider_call(
            credentials,
            "POST",
            "payouts",
            {
                "account_number": credentials["account_number"],
                "fund_account_id": record.fund_account_id,
                "amount": int(record.amount * 100),
                "currency": "INR",
                "mode": "IMPS",
                "purpose": "payout",
                "queue_if_low_balance": True,
                "reference_id": record.id,
                "notes": {"request_id": record.id},
            },
            idempotency=record.id,
        )
        record.provider_id = state["id"]
    if (
        state.get("id") != record.provider_id
        or Decimal(str(state.get("amount", -1))) / 100 != record.amount
        or state.get("currency") != "INR"
        or state.get("fund_account_id") != record.fund_account_id
        or (state.get("reference_id") and state.get("reference_id") != record.id)
    ):
        raise RuntimeError("Provider payout identity or amount mismatch")
    db.scalar(select(User).where(User.id == record.affiliate_id).with_for_update())
    status = state["status"]
    if status == "processed":
        if record.status != "processed":
            db.add(
                Payout(
                    workspace_id=workspace_id,
                    affiliate_id=record.affiliate_id,
                    amount=record.amount,
                    currency="INR",
                    reference="razorpayx:" + record.provider_id,
                    recorded_by=record.requested_by or provider.configured_by,
                )
            )
        record.status = "processed"
    elif status == "reversed":
        if record.status == "processed" and not db.scalar(
            select(PayoutAdjustment.id).where(PayoutAdjustment.request_id == record.id)
        ):
            db.add(
                PayoutAdjustment(
                    workspace_id=workspace_id,
                    affiliate_id=record.affiliate_id,
                    request_id=record.id,
                    amount=record.amount,
                    currency="INR",
                )
            )
        record.status = "reversed"
    elif status in {"failed", "rejected", "cancelled"}:
        if record.status != "processed":
            record.status = "failed"
    elif status in {"queued", "pending", "processing"}:
        if record.status != "processed":
            record.status = "processing"
    else:
        raise RuntimeError("Unsupported provider payout status")
    record.updated_at = now()


@router.post("/webhooks/{provider_id}")
async def payout_webhook(provider_id: str, request: Request, db: DB):
    raw = await request.body()
    return await run_in_threadpool(ingest_webhook, provider_id, raw, dict(request.headers), db)


def ingest_webhook(provider_id, raw, headers, db):
    provider = db.get(PayoutProvider, provider_id)
    if not provider:
        raise HTTPException(404, "Unknown network provider")
    credentials = json.loads(decrypt(provider.credentials))
    expected = hmac.new(credentials["webhook_secret"].encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, headers.get("x-razorpay-signature", "")):
        raise HTTPException(401, "Invalid payout webhook signature")
    key = headers.get("x-razorpay-event-id", "")
    if not key or len(key) > 60:
        raise HTTPException(422, "Event ID required")
    key = "payout:" + provider.id + ":" + key
    # BillingEvent IDs permit 100 characters; hashed event identity remains tenant scoped.
    key = hashlib.sha256(key.encode()).hexdigest()
    if db.get(BillingEvent, key):
        return {"received": True, "duplicate": True}
    try:
        entity = json.loads(raw)["payload"]["payout"]["entity"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "Payout entity required")
    record = db.scalar(
        select(PayoutRequest).where(
            PayoutRequest.workspace_id == provider.workspace_id,
            PayoutRequest.provider_id == entity["id"],
        )
    )
    if not record:
        raise HTTPException(404, "Unknown payout")
    db.add(BillingEvent(id=key))
    enqueue(db, provider.workspace_id, "payout", {"request_id": record.id})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"received": True, "duplicate": True}
    return {"received": True}


def schedule(db):
    providers = list(
        db.scalars(
            select(PayoutProvider)
            .where(PayoutProvider.automatic.is_(True), PayoutProvider.next_run_at <= now())
            .order_by(PayoutProvider.workspace_id)
            .limit(10)
        )
    )
    for provider in providers:
        workspace = db.scalar(
            select(Workspace)
            .where(Workspace.id == provider.workspace_id)
            .with_for_update(skip_locked=True)
        )
        if workspace is None:
            continue
        current = db.scalar(
            select(PayoutProvider).where(PayoutProvider.id == provider.id).with_for_update()
        )
        from .services import utc

        if utc(current.next_run_at) > now():
            continue
        try:
            entitlement(workspace)
        except HTTPException:
            current.next_run_at = now() + timedelta(hours=1)
            continue
        beneficiaries = list(
            db.scalars(
                select(PayoutBeneficiary)
                .where(
                    PayoutBeneficiary.workspace_id == workspace.id,
                    PayoutBeneficiary.id > current.beneficiary_cursor,
                )
                .order_by(PayoutBeneficiary.id)
                .limit(100)
            )
        )
        for beneficiary in beneficiaries:
            recipient = db.scalar(
                select(User).where(User.id == beneficiary.affiliate_id).with_for_update()
            )
            if not recipient.active or (
                recipient.parent_user_id and not db.get(User, recipient.parent_user_id).active
            ):
                continue
            amount = available(db, workspace.id, beneficiary.affiliate_id).quantize(
                Decimal(".01"), rounding=ROUND_DOWN
            )
            if amount >= current.minimum_amount:
                # Deterministic schedule key prevents duplicate reservations after retry.
                key = (
                    "schedule_"
                    + current.next_run_at.date().isoformat().replace("-", "")
                    + "_"
                    + str(int(current.next_run_at.timestamp()))
                    + "_"
                    + beneficiary.affiliate_id
                )
                queue_request(db, workspace.id, beneficiary.affiliate_id, amount, key)
        if len(beneficiaries) == 100:
            current.beneficiary_cursor = beneficiaries[-1].id
        else:
            current.beneficiary_cursor = ""
            current.next_run_at = now() + timedelta(hours=current.interval_hours)


@router.post("/requests/{request_id}/cancel")
def cancel_request(request_id: str, db: DB, user: Actor):
    admin(user)
    record = db.scalar(
        select(PayoutRequest)
        .where(PayoutRequest.id == request_id, PayoutRequest.workspace_id == user.workspace_id)
        .with_for_update()
    )
    if not record:
        raise HTTPException(404, "Payout request not found")
    event = db.scalar(
        select(OutboxEvent).where(OutboxEvent.id == record.outbox_id).with_for_update()
    )
    if (
        record.status != "queued"
        or record.provider_id
        or not event
        or event.attempts != 0
        or event.status != "pending"
    ):
        raise HTTPException(
            409,
            "Submission may have begun; reconcile with the provider instead of cancelling locally",
        )
    record.status = "cancelled"
    event.status = "delivered"
    event.payload = encrypt("{}")
    audit(db, user, "payout.request_cancelled", record.id)
    db.commit()
    return {"cancelled": True}
