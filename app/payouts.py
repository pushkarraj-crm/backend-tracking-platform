from decimal import Decimal
from fastapi import APIRouter, HTTPException, Query
from pydantic import Field
from sqlalchemy import select, func, union_all
from sqlalchemy.exc import IntegrityError
from .models import User, Workspace, CommissionEntry, Payout, PayoutRequest, PayoutAdjustment
from .security import DB, Actor, staff_only
from .schemas import StrictModel
from .services import entitlement, audit
from .routes import visible_users

router = APIRouter(prefix="/payouts", tags=["Payouts"])


class PayoutBody(StrictModel):
    affiliate_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=4)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    reference: str = Field(min_length=1, max_length=120)


def entries(user):
    credits = select(
        CommissionEntry.affiliate_id, CommissionEntry.currency, CommissionEntry.amount
    ).where(CommissionEntry.workspace_id == user.workspace_id)
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
    staff_only(user)
    entitlement(db.get(Workspace, user.workspace_id))
    affiliate = db.scalar(
        select(User)
        .where(User.id == body.affiliate_id, User.workspace_id == user.workspace_id)
        .with_for_update()
    )
    if not affiliate or affiliate.role not in {"affiliate", "publisher"}:
        raise HTTPException(404, "Commission account not found")
    records = entries(user)
    balance = db.scalar(
        select(func.coalesce(func.sum(records.c.amount), 0)).where(
            records.c.affiliate_id == affiliate.id, records.c.currency == body.currency
        )
    )
    if body.amount > balance:
        raise HTTPException(409, "Payout exceeds the available commission balance")
    payout = Payout(workspace_id=user.workspace_id, recorded_by=user.id, **body.model_dump())
    db.add(payout)
    try:
        db.flush()
        audit(db, user, "payout.recorded", payout.id, body.reference)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Payout reference already recorded")
    return {
        "id": payout.id,
        "amount": str(payout.amount),
        "currency": payout.currency,
        "reference": payout.reference,
    }
