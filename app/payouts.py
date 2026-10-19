from decimal import Decimal
from fastapi import APIRouter, HTTPException, Query
from pydantic import Field
from sqlalchemy import select, func, union_all
from .models import CommissionEntry, Payout, PayoutRequest, PayoutAdjustment, Conversion, Offer
from .security import DB, Actor, staff_only
from .schemas import StrictModel
from .routes import visible_users

router = APIRouter(prefix="/payouts", tags=["Payouts"])


class PayoutBody(StrictModel):
    affiliate_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=4)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    reference: str = Field(min_length=1, max_length=120)


def entries(user):
    credits = (
        select(CommissionEntry.affiliate_id, CommissionEntry.currency, CommissionEntry.amount)
        .join(Conversion, Conversion.id == CommissionEntry.conversion_id)
        .join(Offer, Offer.id == Conversion.offer_id)
        .where(CommissionEntry.workspace_id == user.workspace_id, Offer.campaign_model == "legacy")
    )
    debits = select(Payout.affiliate_id, Payout.currency, (-Payout.amount).label("amount")).where(
        Payout.workspace_id == user.workspace_id
    )
    if user.role in {"affiliate", "publisher"}:
        credits = credits.where(CommissionEntry.affiliate_id.in_(visible_users(user)))
        debits = debits.where(Payout.affiliate_id.in_(visible_users(user)))
    else:
        staff_only(user)
    reservations = select(
        PayoutRequest.affiliate_id, PayoutRequest.currency, (-PayoutRequest.amount).label("amount")
    ).where(
        PayoutRequest.workspace_id == user.workspace_id,
        PayoutRequest.status.in_(["queued", "submitting", "processing"]),
    )
    reversals = select(
        PayoutAdjustment.affiliate_id, PayoutAdjustment.currency, PayoutAdjustment.amount
    ).where(PayoutAdjustment.workspace_id == user.workspace_id)
    if user.role in {"affiliate", "publisher"}:
        reservations = reservations.where(PayoutRequest.affiliate_id.in_(visible_users(user)))
        reversals = reversals.where(PayoutAdjustment.affiliate_id.in_(visible_users(user)))
    return union_all(credits, debits, reservations, reversals).subquery()


@router.get("/balances")
def balances(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    records = entries(user)
    query = (
        select(records.c.affiliate_id, records.c.currency, func.sum(records.c.amount))
        .group_by(records.c.affiliate_id, records.c.currency)
        .order_by(records.c.affiliate_id, records.c.currency)
        .offset(offset)
        .limit(limit)
    )
    return [
        {"affiliate_id": affiliate, "currency": currency, "balance": str(amount)}
        for affiliate, currency, amount in db.execute(query)
    ]


@router.get("")
def payout_history(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    query = select(Payout).where(Payout.workspace_id == user.workspace_id)
    if user.role in {"affiliate", "publisher"}:
        query = query.where(Payout.affiliate_id.in_(visible_users(user)))
    else:
        staff_only(user)
    return [
        {
            "id": p.id,
            "affiliate_id": p.affiliate_id,
            "amount": str(p.amount),
            "currency": p.currency,
            "reference": p.reference,
            "created_at": p.created_at,
        }
        for p in db.scalars(
            query.order_by(Payout.created_at.desc(), Payout.id).offset(offset).limit(limit)
        )
    ]


@router.post("", status_code=201)
def record_payout(body: PayoutBody, db: DB, user: Actor):
    raise HTTPException(410, "Use publisher invoices and external payment confirmation")
