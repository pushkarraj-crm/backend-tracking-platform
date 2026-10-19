import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Literal, Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, case
from .db import SessionLocal
from .models import Click, TrackingLink, Offer, Conversion
from .network_models import Impression
from .security import DB, Actor
from .routes import visible_users, entitled

router = APIRouter(prefix="/reports", tags=["Detailed reports"])


class Filters(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    offer_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    affiliate_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    sub_id: str | None = Field(default=None, max_length=100)
    status: Literal["pending", "approved", "rejected"] | None = None
    event_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    source: str | None = Field(default=None, max_length=256)
    aff_click_id: str | None = Field(default=None, max_length=256)
    sub_aff_id: str | None = Field(default=None, max_length=256)
    aff_sub1: str | None = Field(default=None, max_length=256)
    aff_sub2: str | None = Field(default=None, max_length=256)
    aff_sub3: str | None = Field(default=None, max_length=256)
    aff_sub4: str | None = Field(default=None, max_length=256)
    aff_sub5: str | None = Field(default=None, max_length=256)
    aff_sub6: str | None = Field(default=None, max_length=256)
    aff_sub7: str | None = Field(default=None, max_length=256)
    aff_sub8: str | None = Field(default=None, max_length=256)
    aff_sub9: str | None = Field(default=None, max_length=256)
    aff_sub10: str | None = Field(default=None, max_length=256)


FilterDep = Annotated[Filters, Depends()]


def window(filters):
    end = filters.end or datetime.now(timezone.utc)
    start = filters.start or end - timedelta(days=7)
    if not start.tzinfo or not end.tzinfo or start >= end or end - start > timedelta(days=366):
        raise HTTPException(422, "Use timezone-aware start/end spanning no more than 366 days")
    return start, end


def scope(query, user, filters, traffic=Click):
    query = query.where(traffic.workspace_id == user.workspace_id)
    if user.role in {"affiliate", "publisher"}:
        query = query.where(TrackingLink.affiliate_id.in_(visible_users(user)))
    elif user.role == "advertiser":
        query = query.where(Offer.advertiser_id == user.id)
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Report access required")
    if filters.offer_id:
        query = query.where(TrackingLink.offer_id == filters.offer_id)
    if filters.affiliate_id:
        query = query.where(TrackingLink.affiliate_id == filters.affiliate_id)
    if filters.country:
        query = query.where(traffic.country == filters.country)
    if filters.sub_id:
        query = query.where(traffic.sub_id == filters.sub_id)
    for key in ["source", "aff_click_id", "sub_aff_id", *[f"aff_sub{i}" for i in range(1, 11)]]:
        if getattr(filters, key) is not None:
            query = query.where(traffic.attributes[key].as_string() == getattr(filters, key))
    if filters.event_key or filters.status:
        if traffic is Impression:
            raise HTTPException(
                422, "Event/status filters apply to clicks and conversions, not impressions"
            )
        event_query = select(Conversion.id).where(Conversion.click_id == Click.id)
        if filters.event_key:
            event_query = event_query.where(Conversion.event_key == filters.event_key)
        if filters.status:
            event_query = event_query.where(Conversion.status == filters.status)
        query = query.where(event_query.correlate(Click).exists())
    return query


def detail_query(dataset, user, filters):
    start, end = window(filters)
    if dataset == "impressions":
        query = (
            select(
                Impression.id,
                TrackingLink.offer_id,
                TrackingLink.affiliate_id,
                Impression.sub_id,
                Impression.country,
                Impression.device,
                Impression.operating_system,
                Impression.attributes,
                Impression.created_at,
            )
            .join(TrackingLink, TrackingLink.id == Impression.link_id)
            .join(Offer, Offer.id == TrackingLink.offer_id)
            .where(Impression.created_at >= start, Impression.created_at < end)
            .order_by(Impression.created_at, Impression.id)
        )
        return scope(query, user, filters, Impression)
    if dataset == "clicks":
        query = (
            select(
                Click.id,
                TrackingLink.offer_id,
                TrackingLink.affiliate_id,
                Click.sub_id,
                Click.country,
                Click.device,
                Click.operating_system,
                Click.language,
                Click.created_at,
                Click.attributes,
            )
            .join(TrackingLink, Click.link_id == TrackingLink.id)
            .join(Offer, TrackingLink.offer_id == Offer.id)
        )
        query = query.where(Click.created_at >= start, Click.created_at < end).order_by(
            Click.created_at, Click.id
        )
    else:
        columns = [
            Conversion.id,
            Conversion.click_id,
            Conversion.offer_id,
            TrackingLink.affiliate_id,
            Conversion.transaction_id,
            Conversion.event_key,
            Conversion.event_type,
            Conversion.sale_amount,
            Conversion.status,
            Conversion.currency,
            Conversion.created_at,
        ]
        if user.role not in {"affiliate", "publisher"}:
            columns.append(Conversion.revenue)
        if user.role != "advertiser":
            columns.append(Conversion.payout)
        query = (
            select(*columns)
            .join(Click, Conversion.click_id == Click.id)
            .join(TrackingLink, Click.link_id == TrackingLink.id)
            .join(Offer, TrackingLink.offer_id == Offer.id)
        )
        query = query.where(Conversion.created_at >= start, Conversion.created_at < end).order_by(
            Conversion.created_at, Conversion.id
        )
        if filters.status:
            query = query.where(Conversion.status == filters.status)
        if filters.event_key:
            query = query.where(Conversion.event_key == filters.event_key)
    return scope(query, user, filters)


def serialize(row):
    from decimal import Decimal

    return {k: str(v) if isinstance(v, Decimal) else v for k, v in row.items()}


@router.get("/clicks")
def clicks(
    db: DB,
    user: Actor,
    filters: FilterDep,
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(50, ge=1, le=100),
):
    entitled(db, user)
    return [
        serialize(row)
        for row in db.execute(
            detail_query("clicks", user, filters).offset(offset).limit(limit)
        ).mappings()
    ]


@router.get("/conversions")
def conversions(
    db: DB,
    user: Actor,
    filters: FilterDep,
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(50, ge=1, le=100),
):
    entitled(db, user)
    return [
        serialize(row)
        for row in db.execute(
            detail_query("conversions", user, filters).offset(offset).limit(limit)
        ).mappings()
    ]


@router.get("/impressions")
def impressions(
    db: DB,
    user: Actor,
    filters: FilterDep,
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(50, ge=1, le=100),
):
    entitled(db, user)
    return [
        serialize(row)
        for row in db.execute(
            detail_query("impressions", user, filters).offset(offset).limit(limit)
        ).mappings()
    ]


@router.get("/grouped")
def grouped(
    db: DB,
    user: Actor,
    filters: FilterDep,
    group_by: Literal[
        "offer",
        "affiliate",
        "sub_id",
        "country",
        "device",
        "operating_system",
        "day",
        "hour",
        "event",
        "source",
    ] = "offer",
    limit: int = Query(100, ge=1, le=1000),
):
    entitled(db, user)
    start, end = window(filters)
    dimensions = {
        "offer": TrackingLink.offer_id,
        "affiliate": TrackingLink.affiliate_id,
        "sub_id": Click.sub_id,
        "country": Click.country,
        "device": Click.device,
        "operating_system": Click.operating_system,
        "event": Conversion.event_key,
        "source": Click.attributes["source"].as_string(),
    }
    if group_by in {"day", "hour"}:
        dimension = (
            func.date_trunc(group_by, Click.created_at)
            if db.bind.dialect.name == "postgresql"
            else func.strftime(
                "%Y-%m-%d" if group_by == "day" else "%Y-%m-%dT%H:00:00", Click.created_at
            )
        )
    else:
        dimension = dimensions[group_by]
    approved = case((Conversion.status == "approved", 1), else_=0)
    columns = [
        dimension.label("group"),
        Offer.currency,
        func.count(func.distinct(Click.id)).label("clicks"),
        func.count(Conversion.id).label("conversions"),
        func.sum(approved).label("approved_conversions"),
        func.count(
            func.distinct(case((Conversion.status == "approved", Click.id), else_=None))
        ).label("approved_unique_clicks"),
    ]
    if user.role not in {"affiliate", "publisher"}:
        columns.append(
            func.sum(case((Conversion.status == "approved", Conversion.revenue), else_=0)).label(
                "revenue"
            )
        )
    if user.role != "advertiser":
        columns.append(
            func.sum(case((Conversion.status == "approved", Conversion.payout), else_=0)).label(
                "payout"
            )
        )
    query = (
        select(*columns)
        .select_from(Click)
        .join(TrackingLink, Click.link_id == TrackingLink.id)
        .join(Offer, TrackingLink.offer_id == Offer.id)
        .outerjoin(Conversion, Conversion.click_id == Click.id)
        .where(Click.created_at >= start, Click.created_at < end)
    )
    query = scope(query, user, filters)
    if filters.status:
        query = query.where(Conversion.status == filters.status)
    if filters.event_key:
        query = query.where(Conversion.event_key == filters.event_key)
    return [
        serialize(row)
        for row in db.execute(
            query.group_by(dimension, Offer.currency)
            .order_by(dimension, Offer.currency)
            .limit(limit)
        ).mappings()
    ]


def cell(value):
    text = (
        value.isoformat() if isinstance(value, datetime) else ("" if value is None else str(value))
    )
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        text = "'" + text
    return text


@router.get("/export/{dataset}")
def export(
    dataset: Literal["clicks", "conversions", "impressions"],
    db: DB,
    user: Actor,
    filters: FilterDep,
    max_rows: int = Query(10000, ge=1, le=100000),
):
    entitled(db, user)
    query = detail_query(dataset, user, filters).limit(max_rows + 1)
    observed = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))

    def stream():
        with SessionLocal() as session:
            result = session.execute(query.execution_options(yield_per=500)).mappings()
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(result.keys())
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)
            for index, row in enumerate(result):
                if index == max_rows:
                    break
                writer.writerow([cell(v) for v in row.values()])
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)

    return StreamingResponse(
        stream(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{dataset}.csv"',
            "X-Export-Max-Rows": str(max_rows),
            "X-Export-Rows": str(min(observed, max_rows)),
            "X-Export-Truncated": "true" if observed > max_rows else "false",
            "X-Export-Window": "UTC; bounded by supplied start/end",
        },
    )
