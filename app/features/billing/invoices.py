"""Direct advertiser/publisher invoices. No payment-provider calls or money transfers."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, HttpUrl, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.core.security import DB, Actor
from app.core.services import audit
from app.permissions.resources import entitled
from app.persistence.models import (
    Click,
    Conversion,
    InvoiceEvent,
    InvoiceLine,
    InvoiceReservation,
    Offer,
    PublisherInvoice,
    TrackingLink,
    User,
)
from app.shared.schemas import StrictModel

router = APIRouter(prefix="/publisher-invoices", tags=["Direct publisher invoices"])


class InvoiceBody(StrictModel):
    number: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9/_.-]*$")
    conversion_ids: list[str] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_ids(self):
        import re

        if len(set(self.conversion_ids)) != len(self.conversion_ids) or any(
            not re.fullmatch(r"[a-f0-9]{32}", i) for i in self.conversion_ids
        ):
            raise ValueError("Select unique conversion IDs")
        return self


class InvoiceAction(StrictModel):
    action: Literal["approve", "reject", "report_payment", "confirm_receipt", "dispute", "cancel"]
    note: str = Field(default="", max_length=1000)
    payment_reference: str | None = Field(default=None, min_length=1, max_length=200)
    receipt_url: HttpUrl | None = None

    @model_validator(mode="after")
    def payment(self):
        if self.action == "report_payment" and (
            not self.payment_reference or not self.payment_reference.strip()
        ):
            raise ValueError("Provide the reference for the external payment")
        if self.action in {"reject", "dispute"} and not self.note.strip():
            raise ValueError("Provide a reason")
        if self.receipt_url and (
            self.receipt_url.scheme != "https"
            or self.receipt_url.username
            or self.receipt_url.password
        ):
            raise ValueError("Receipt link must use HTTPS without credentials")
        if self.receipt_url and len(str(self.receipt_url)) > 2048:
            raise ValueError("Receipt link is too long")
        if self.action != "report_payment" and (self.payment_reference or self.receipt_url):
            raise ValueError("Payment evidence is only accepted when reporting a payment")
        return self


def visible(query, user):
    query = query.where(PublisherInvoice.workspace_id == user.workspace_id)
    if user.role == "publisher":
        return query.where(PublisherInvoice.publisher_id == user.id)
    if user.role == "advertiser":
        return query.where(PublisherInvoice.advertiser_id == user.id)
    if user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Invoice access required")
    return query


def output(invoice):
    result = {
        k: getattr(invoice, k)
        for k in (
            "id",
            "number",
            "advertiser_id",
            "publisher_id",
            "currency",
            "status",
            "payment_reference",
            "receipt_url",
            "reported_at",
            "confirmed_at",
            "created_at",
        )
    }
    result["amount"] = str(invoice.amount)
    result["payment_method"] = "external_direct"
    return result


def eligible_query(user, status="approved"):
    query = (
        select(Conversion, Offer.advertiser_id, TrackingLink.affiliate_id)
        .join(Offer, Conversion.offer_id == Offer.id)
        .join(Click, Click.id == Conversion.click_id)
        .join(TrackingLink, TrackingLink.id == Click.link_id)
        .where(
            Conversion.workspace_id == user.workspace_id,
            Conversion.status == status,
            Offer.campaign_model.in_(["CPL", "CPS"]),
            Offer.advertiser_id.is_not(None),
            Conversion.payout > 0,
            ~select(InvoiceReservation.conversion_id)
            .where(InvoiceReservation.conversion_id == Conversion.id)
            .exists(),
        )
    )
    if user.role == "publisher":
        query = query.where(TrackingLink.affiliate_id == user.id)
    elif user.role == "advertiser":
        query = query.where(Offer.advertiser_id == user.id)
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Invoice access required")
    return query


@router.get("/eligible-conversions")
def eligible(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500)
):
    return [
        {
            "id": c.id,
            "offer_id": c.offer_id,
            "transaction_id": c.transaction_id,
            "advertiser_id": advertiser,
            "publisher_id": publisher,
            "currency": c.currency,
            "amount": str(c.payout),
            "created_at": c.created_at,
        }
        for c, advertiser, publisher in db.execute(
            eligible_query(user)
            .order_by(Conversion.created_at, Conversion.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@router.get("/summary")
def summary(db: DB, user: Actor):
    groups = db.execute(
        visible(
            select(
                PublisherInvoice.currency,
                PublisherInvoice.status,
                func.sum(PublisherInvoice.amount),
            ),
            user,
        ).group_by(PublisherInvoice.currency, PublisherInvoice.status)
    )
    result = [
        {"currency": currency, "status": status, "amount": str(amount)}
        for currency, status, amount in groups
    ]
    pending = eligible_query(user, status="pending").subquery()
    for currency, amount in db.execute(
        select(pending.c.currency, func.sum(pending.c.payout)).group_by(pending.c.currency)
    ):
        result.append({"currency": currency, "status": "pending_approval", "amount": str(amount)})
    payable = eligible_query(user).subquery()
    for currency, amount in db.execute(
        select(payable.c.currency, func.sum(payable.c.payout)).group_by(payable.c.currency)
    ):
        result.append({"currency": currency, "status": "payable", "amount": str(amount)})
    return {
        "settlement": "Advertisers pay publishers outside the platform. Only publisher confirmation marks an invoice paid.",
        "totals": result,
    }


@router.post("", status_code=201)
def create_invoice(body: InvoiceBody, db: DB, user: Actor):
    if user.role != "publisher":
        raise HTTPException(403, "Only publishers may submit their invoices")
    entitled(db, user)
    previous = db.scalar(
        select(PublisherInvoice).where(
            PublisherInvoice.workspace_id == user.workspace_id,
            PublisherInvoice.publisher_id == user.id,
            PublisherInvoice.number == body.number,
        )
    )
    if previous:
        previous_ids = set(
            db.scalars(
                select(InvoiceLine.conversion_id).where(InvoiceLine.invoice_id == previous.id)
            )
        )
        if previous_ids != set(body.conversion_ids):
            raise HTTPException(409, "Invoice number already used for different conversions")
        return output(previous)
    # Lock conversions in a deterministic order before the publisher account,
    # matching conversion approval's lock order and avoiding duplicate billing.
    rows = list(
        db.execute(
            eligible_query(user)
            .where(Conversion.id.in_(body.conversion_ids))
            .order_by(Conversion.id)
            .with_for_update(of=Conversion)
        )
    )
    if len(rows) != len(body.conversion_ids):
        raise HTTPException(
            409, "Conversions must be approved, unpaid and belong to this publisher"
        )
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    advertisers = {a for _, a, _ in rows}
    currencies = {c.currency for c, _, _ in rows}
    if len(advertisers) != 1 or len(currencies) != 1:
        raise HTTPException(422, "An invoice must contain one advertiser and one currency")
    advertiser_id = next(iter(advertisers))
    advertiser = db.get(User, advertiser_id)
    if not advertiser or not advertiser.active or advertiser.role != "advertiser":
        raise HTTPException(409, "Advertiser account is unavailable")
    invoice = PublisherInvoice(
        workspace_id=user.workspace_id,
        advertiser_id=advertiser_id,
        publisher_id=user.id,
        number=body.number,
        currency=next(iter(currencies)),
        amount=sum((c.payout for c, _, _ in rows), Decimal("0")),
    )
    db.add(invoice)
    try:
        db.flush()
        for conversion, _, _ in rows:
            db.add(
                InvoiceLine(
                    workspace_id=user.workspace_id,
                    invoice_id=invoice.id,
                    conversion_id=conversion.id,
                    amount=conversion.payout,
                )
            )
            db.add(
                InvoiceReservation(
                    workspace_id=user.workspace_id,
                    invoice_id=invoice.id,
                    conversion_id=conversion.id,
                )
            )
        event(db, user, invoice, "submitted", "Submitted approved conversions")
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            409, "Invoice number or conversions have already been invoiced"
        ) from None
    return output(invoice)


def event(db, user, invoice, action, detail, evidence=None):
    db.add(
        InvoiceEvent(
            workspace_id=user.workspace_id,
            invoice_id=invoice.id,
            actor_id=user.id,
            action=action,
            detail=detail,
            evidence=evidence or {},
        )
    )
    audit(db, user, "invoice." + action, invoice.id)
    from app.features.accounts.notifications import notify

    notify(
        db,
        user.workspace_id,
        [invoice.advertiser_id, invoice.publisher_id],
        "invoice.updated",
        "Publisher invoice updated",
        action,
        invoice.id,
    )


@router.get("")
def list_invoices(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    advertiser, publisher = aliased(User), aliased(User)
    query = (
        select(PublisherInvoice, advertiser.email, publisher.email)
        .join(advertiser, advertiser.id == PublisherInvoice.advertiser_id)
        .join(publisher, publisher.id == PublisherInvoice.publisher_id)
    )
    return [
        {**output(invoice), "advertiser_email": adv_email, "publisher_email": pub_email}
        for invoice, adv_email, pub_email in db.execute(
            visible(query, user)
            .order_by(PublisherInvoice.created_at.desc(), PublisherInvoice.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@router.get("/{invoice_id}")
def invoice_detail(invoice_id: str, db: DB, user: Actor):
    invoice = db.scalar(
        visible(select(PublisherInvoice).where(PublisherInvoice.id == invoice_id), user)
    )
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    return {
        **output(invoice),
        "lines": [
            {"conversion_id": line.conversion_id, "amount": str(line.amount)}
            for line in db.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id))
        ],
        "events": [
            {
                k: getattr(e, k)
                for k in ("id", "actor_id", "action", "detail", "evidence", "created_at")
            }
            for e in db.scalars(
                select(InvoiceEvent)
                .where(InvoiceEvent.invoice_id == invoice.id)
                .order_by(InvoiceEvent.created_at, InvoiceEvent.id)
            )
        ],
    }


@router.post("/{invoice_id}/actions")
def invoice_action(invoice_id: str, body: InvoiceAction, db: DB, user: Actor):
    invoice = db.scalar(
        visible(
            select(PublisherInvoice).where(PublisherInvoice.id == invoice_id), user
        ).with_for_update()
    )
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    advertiser_action = body.action in {"approve", "reject", "report_payment"}
    if (advertiser_action and (user.role != "advertiser" or user.id != invoice.advertiser_id)) or (
        not advertiser_action and (user.role != "publisher" or user.id != invoice.publisher_id)
    ):
        raise HTTPException(403, "Only the invoice advertiser or publisher can perform this action")
    transitions = {
        "approve": ({"submitted"}, "approved"),
        "reject": ({"submitted"}, "rejected"),
        "report_payment": ({"approved", "disputed"}, "payment_reported"),
        "confirm_receipt": ({"payment_reported"}, "paid"),
        "dispute": ({"payment_reported"}, "disputed"),
        "cancel": ({"submitted"}, "cancelled"),
    }
    expected, target = transitions[body.action]
    if invoice.status not in expected:
        raise HTTPException(409, "Invoice status changed or action is not allowed")
    invoice.status = target
    detail = body.note
    if body.action == "report_payment":
        invoice.payment_reference = body.payment_reference.strip()
        invoice.receipt_url = str(body.receipt_url) if body.receipt_url else None
        invoice.reported_at = datetime.now(timezone.utc)
        detail = invoice.payment_reference + (": " + body.note if body.note else "")
    elif body.action == "confirm_receipt":
        invoice.confirmed_at = datetime.now(timezone.utc)
    elif body.action in {"reject", "cancel"}:
        db.execute(delete(InvoiceReservation).where(InvoiceReservation.invoice_id == invoice.id))
    event(
        db,
        user,
        invoice,
        body.action,
        detail[:1000],
        {
            "payment_reference": invoice.payment_reference,
            "receipt_url": invoice.receipt_url,
            "note": body.note,
        }
        if body.action == "report_payment"
        else {},
    )
    db.commit()
    return output(invoice)
