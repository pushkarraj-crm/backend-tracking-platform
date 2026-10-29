"""Bounded operator tools: imports, previews, health checks, smart links and automation."""

import csv
import http.client
import io
import ipaddress
import socket
import ssl
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from urllib.parse import urljoin, urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import Field, ValidationError, model_validator
from sqlalchemy import func, select

from app.core.security import DB, Actor
from app.core.services import audit, entitlement
from app.features.campaigns.policy import (
    can_discover,
    destination,
    parameters,
    safe_url,
)
from app.features.campaigns.schemas import CampaignBody
from app.features.campaigns.service import campaign_output, create_campaign_record
from app.permissions.campaigns import manage_campaign
from app.permissions.employees import employee_capability
from app.permissions.resources import owned
from app.persistence.models import (
    Click,
    Conversion,
    InvoiceReservation,
    Offer,
    TrackingLink,
    User,
    Workspace,
)
from app.shared.schemas import StrictModel

router = APIRouter(tags=["Network tools"])


class ImportBody(StrictModel):
    csv_content: str = Field(min_length=1, max_length=60000)
    dry_run: bool = True


@router.post("/tools/import-campaigns")
def import_campaigns(body: ImportBody, db: DB, user: Actor):
    if user.role not in {"admin", "staff", "advertiser"}:
        raise HTTPException(403, "Campaign management role required")
    employee_capability(user, "campaigns:write")
    workspace = db.get(Workspace, user.workspace_id)
    entitlement(workspace)
    stream = csv.DictReader(io.StringIO(body.csv_content.lstrip("\ufeff")))
    allowed = set(CampaignBody.model_fields) - {"events"}
    fields = stream.fieldnames or []
    if not fields or len(fields) != len(set(fields)) or set(fields) - allowed:
        raise HTTPException(422, "CSV must use unique supported campaign field headings")
    rows = list(stream)
    if not rows or len(rows) > 100:
        raise HTTPException(422, "Import 1–100 campaigns per request")
    valid = []
    errors = []
    for number, row in enumerate(rows, start=2):
        try:
            if None in row:
                raise ValueError("CSV row has too many values")
            campaign = CampaignBody(**{k: v for k, v in row.items() if v not in {None, ""}})
            owner_id = user.id if user.role == "advertiser" else campaign.advertiser_id
            if user.role == "advertiser" and campaign.advertiser_id not in {None, user.id}:
                raise ValueError("Cannot import another advertiser's campaigns")
            owner = db.get(User, owner_id) if owner_id else None
            if (
                not owner
                or owner.workspace_id != user.workspace_id
                or not owner.active
                or owner.role != "advertiser"
            ):
                raise ValueError("Select an active advertiser in this network")
            valid.append(campaign)
        except (ValidationError, ValueError) as error:
            details = (
                [
                    {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                    for e in error.errors()
                ]
                if isinstance(error, ValidationError)
                else [{"field": "row", "message": str(error)}]
            )
            errors.append({"row": number, "errors": details})
    from app.core.config import get_settings

    maximum = get_settings().plan_limits.get(workspace.plan, {}).get("offers", 0)
    count = db.scalar(
        select(func.count()).select_from(Offer).where(Offer.workspace_id == user.workspace_id)
    )
    if count + len(valid) > maximum:
        errors.append(
            {
                "row": None,
                "errors": [
                    {
                        "field": "quota",
                        "message": "Import would exceed the current plan campaign limit",
                    }
                ],
            }
        )
    if body.dry_run:
        return {
            "dry_run": True,
            "rows": len(rows),
            "valid_rows": len(valid),
            "errors": errors,
            "can_import": not errors,
        }
    if errors:
        raise HTTPException(
            422, {"message": "Fix CSV preview errors before importing", "errors": errors}
        )
    created = []
    try:
        for campaign in valid:
            offer, key = create_campaign_record(campaign, db, user)
            created.append({**campaign_output(offer, user), "postback_key": key})
        audit(db, user, "campaigns.imported", workspace.id, str(len(created)))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"dry_run": False, "imported": len(created), "campaigns": created}


class BulkStatusBody(StrictModel):
    conversion_ids: list[str] = Field(min_length=1, max_length=100)
    status: Literal["approved", "rejected"]

    @model_validator(mode="after")
    def valid(self):
        import re

        if len(set(self.conversion_ids)) != len(self.conversion_ids) or any(
            not re.fullmatch(r"[a-f0-9]{32}", i) for i in self.conversion_ids
        ):
            raise ValueError("Select unique conversion IDs")
        return self


@router.post("/tools/conversions/status")
def bulk_conversion_status(body: BulkStatusBody, db: DB, user: Actor):
    if user.role not in {"admin", "staff", "advertiser"}:
        raise HTTPException(403, "Conversion review role required")
    employee_capability(user, "conversions:review")
    entitlement(db.get(Workspace, user.workspace_id))
    query = (
        select(Conversion)
        .join(Offer, Offer.id == Conversion.offer_id)
        .where(Conversion.workspace_id == user.workspace_id, Conversion.id.in_(body.conversion_ids))
        .order_by(Conversion.id)
        .with_for_update(of=Conversion)
    )
    if user.role == "advertiser":
        query = query.where(Offer.advertiser_id == user.id)
    rows = list(db.scalars(query))
    if len(rows) != len(body.conversion_ids):
        raise HTTPException(404, "One or more conversions are unavailable")
    reserved = set(
        db.scalars(
            select(InvoiceReservation.conversion_id).where(
                InvoiceReservation.workspace_id == user.workspace_id,
                InvoiceReservation.conversion_id.in_(body.conversion_ids),
            )
        )
    )
    if any(r.id in reserved and r.status != body.status for r in rows):
        raise HTTPException(409, "An invoiced conversion cannot be changed")
    publisher_ids = list(
        db.scalars(
            select(TrackingLink.affiliate_id)
            .join(Click, Click.link_id == TrackingLink.id)
            .join(Conversion, Conversion.click_id == Click.id)
            .where(Conversion.id.in_(body.conversion_ids))
        )
    )
    list(
        db.scalars(
            select(User).where(User.id.in_(publisher_ids)).order_by(User.id).with_for_update()
        )
    )
    from app.features.conversions.service import transition

    for row in rows:
        transition(db, user, row, body.status)
    db.commit()
    return {"updated": len(rows), "status": body.status}


class MisAction(StrictModel):
    event_key: str = Field(default="initial", pattern=r"^[a-z][a-z0-9_]{0,63}$")
    status: Literal["approved", "rejected"] | None = None


class MisImportBody(StrictModel):
    campaign_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    # shortcut: bounded by max_request_bytes (~600 MIS rows); split larger files or raise the limit.
    csv_content: str = Field(min_length=1, max_length=60000)
    reference_column: str = Field(min_length=1, max_length=100)
    status_column: str = Field(min_length=1, max_length=100)
    click_id_column: str | None = Field(default=None, min_length=1, max_length=100)
    amount_column: str | None = Field(default=None, min_length=1, max_length=100)
    statuses: dict[str, MisAction] = Field(min_length=1, max_length=50)
    dry_run: bool = True


@router.post("/tools/conversions/import")
def import_conversions(body: MisImportBody, db: DB, user: Actor):
    """Apply an advertiser MIS report: match rows by our click ID or the advertiser reference."""
    from decimal import InvalidOperation

    from sqlalchemy.exc import IntegrityError

    from app.features.conversions.service import approve_lead as shared_approve_lead
    from app.features.conversions.service import ingest, transition
    from app.shared.schemas import Postback

    employee_capability(user, "conversions:review")
    offer = manage_campaign(db, user, body.campaign_id)
    entitlement(db.get(Workspace, user.workspace_id))
    stream = csv.DictReader(io.StringIO(body.csv_content.lstrip("﻿")))
    fields = stream.fieldnames or []
    needed = {body.reference_column, body.status_column} | {
        c for c in (body.click_id_column, body.amount_column) if c
    }
    if len(fields) != len(set(fields)) or needed - set(fields):
        raise HTTPException(422, "CSV must have unique headings including every mapped column")
    rows = list(stream)
    if not rows:
        raise HTTPException(422, "CSV has no data rows")
    counts = {"created": 0, "updated": 0, "unchanged": 0, "ignored": 0, "leads_approved": 0}

    def approve_lead(reference, action):
        if action.status == "approved" and shared_approve_lead(
            db, user, offer, reference, action.event_key
        ):
            counts["leads_approved"] += 1

    unmatched, errors = [], []
    for number, row in enumerate(rows, start=2):
        reference = (row.get(body.reference_column) or "").strip()
        try:
            if None in row:
                raise ValueError("CSV row has too many values")
            if not reference:
                raise ValueError("Reference is empty")
            action = body.statuses.get((row.get(body.status_column) or "").strip())
            if not action:
                counts["ignored"] += 1
                continue
            existing = db.scalar(
                select(Conversion)
                .where(
                    Conversion.workspace_id == offer.workspace_id,
                    Conversion.offer_id == offer.id,
                    Conversion.transaction_id == reference,
                    Conversion.event_key == action.event_key,
                )
                .with_for_update()
            )
            if existing:
                if action.status and existing.status != action.status:
                    transition(db, user, existing, action.status)
                    counts["updated"] += 1
                else:
                    counts["unchanged"] += 1
                approve_lead(reference, action)
                continue
            click_id = (row.get(body.click_id_column) or "").strip() if body.click_id_column else ""
            if not click_id:
                # A later funnel stage inherits the click of an earlier conversion for this reference.
                click_id = db.scalar(
                    select(Conversion.click_id)
                    .where(
                        Conversion.workspace_id == offer.workspace_id,
                        Conversion.offer_id == offer.id,
                        Conversion.transaction_id == reference,
                    )
                    .order_by(Conversion.created_at)
                    .limit(1)
                )
            if action.status == "rejected" or not click_id:
                unmatched.append({"row": number, "reference": reference})
                continue
            amount = (row.get(body.amount_column) or "").strip() if body.amount_column else ""
            conversion, _ = ingest(
                db,
                offer,
                Postback(
                    click_id=click_id,
                    transaction_id=reference,
                    event_key=action.event_key,
                    event_type="sale" if amount else None,
                    sale_amount=Decimal(amount.replace(",", "")) if amount else None,
                ),
                offline=True,
            )
            if action.status and conversion.status != action.status:
                transition(db, user, conversion, action.status)
            counts["created"] += 1
            approve_lead(reference, action)
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Conversions changed during import; retry") from None
        except HTTPException as error:
            errors.append({"row": number, "reference": reference, "message": str(error.detail)})
        except ValidationError as error:
            message = "; ".join(e["msg"] for e in error.errors())
            errors.append({"row": number, "reference": reference, "message": message})
        except (ValueError, InvalidOperation) as error:
            message = str(error) if isinstance(error, ValueError) else "Amount is not a number"
            errors.append({"row": number, "reference": reference, "message": message})
    result = {
        "dry_run": body.dry_run,
        "rows": len(rows),
        **counts,
        "unmatched": unmatched,
        "errors": errors,
        "can_import": not errors,
    }
    # Rows are applied in one transaction so the preview is exact and a failed import changes nothing.
    if body.dry_run or errors:
        db.rollback()
        if errors and not body.dry_run:
            raise HTTPException(
                422, {"message": "Fix MIS preview errors before importing", **result}
            )
        return result
    audit(
        db,
        user,
        "conversions.imported",
        offer.id,
        f"{counts['created']} created, {counts['updated']} updated",
    )
    db.commit()
    return result


class PreviewBody(StrictModel):
    publisher_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    source_parameters: dict[str, str] = Field(default_factory=dict, max_length=30)
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    device: Literal["desktop", "mobile", "tablet", "unknown"] = "unknown"


@router.post("/campaigns/{campaign_id}/url-preview")
def url_preview(campaign_id: str, body: PreviewBody, db: DB, user: Actor):
    offer = owned(db, Offer, campaign_id, user)
    if not can_discover(db, user, offer):
        raise HTTPException(404, "Campaign not found")
    publisher_id = body.publisher_id or user.id
    if user.role not in {"admin", "staff", "advertiser"} and publisher_id != user.id:
        raise HTTPException(403, "Can preview only your own tracking context")
    if user.role == "advertiser" and offer.advertiser_id != user.id:
        raise HTTPException(404, "Campaign not found")
    link = db.scalar(
        select(TrackingLink).where(
            TrackingLink.workspace_id == user.workspace_id,
            TrackingLink.offer_id == offer.id,
            TrackingLink.affiliate_id == publisher_id,
        )
    )
    if not link:
        raise HTTPException(422, "Select a publisher with an existing campaign link")
    attrs = parameters(body.source_parameters)
    click = Click(
        id="0" * 32,
        workspace_id=user.workspace_id,
        link_id=link.id,
        created_at=datetime.now(timezone.utc),
        attributes=attrs,
        country=body.country,
        device=body.device,
        operating_system="other",
        language=None,
        sub_id=None,
    )
    url = destination(db, offer, link, click)
    return {
        "mode": "preview_only",
        "records_click": False,
        "example_click_id": "0" * 32,
        "advertiser_url": url,
    }


def public_addresses(host):
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.timeout = 1
    resolver.lifetime = 2
    addresses = []
    for kind in ["A", "AAAA"]:
        try:
            addresses.extend(str(record) for record in resolver.resolve(host, kind))
        except dns.resolver.NoAnswer:
            pass
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError("Destination must resolve only to public addresses")
    return sorted(addresses)


def health_probe(url):
    started = time.monotonic()
    chain = []
    seen = set()
    for _ in range(5):
        if time.monotonic() - started > 12:
            return {"healthy": False, "reason": "time_limit", "chain": chain}
        safe_url(url)
        parts = urlsplit(url)
        if url in seen:
            return {"healthy": False, "reason": "redirect_loop", "chain": chain}
        seen.add(url)
        address = public_addresses(parts.hostname)[0]
        connection = http.client.HTTPSConnection(parts.hostname, 443, timeout=4)
        connection.sock = ssl.create_default_context().wrap_socket(
            socket.create_connection((address, 443), timeout=4), server_hostname=parts.hostname
        )
        try:
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            connection.request("HEAD", path, headers={"User-Agent": "Relay-Link-Health/1.0"})
            response = connection.getresponse()
            location = response.getheader("Location")
            status = response.status
        finally:
            connection.close()
        from app.features.callbacks.router import output

        # Returned diagnostics do not expose common query credential fields.
        class Endpoint:
            id = ""
            affiliate_id = ""
            offer_id = None
            method = "HEAD"
            active = True

        endpoint = Endpoint()
        endpoint.url = url
        safe_display = output(endpoint)["url"]
        chain.append({"url": safe_display, "status": status})
        if 300 <= status < 400 and location:
            url = urljoin(url, location)
            continue
        return {
            "healthy": 200 <= status < 300,
            "reason": "ok" if 200 <= status < 300 else "http_status",
            "chain": chain,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    return {"healthy": False, "reason": "redirect_limit", "chain": chain}


@router.post("/tools/campaigns/{campaign_id}/link-health")
def link_health(campaign_id: str, db: DB, user: Actor):
    offer = manage_campaign(db, user, campaign_id)
    from urllib.parse import unquote

    from app.features.campaigns.policy import TOKEN

    if TOKEN.search(unquote(offer.destination_url)):
        raise HTTPException(
            422,
            "Use URL preview to inspect macros; this health check requires a stable destination URL",
        )
    try:
        result = health_probe(offer.destination_url)
    except Exception:
        result = {
            "healthy": False,
            "reason": "dns_tls_or_destination_validation_failed",
            "chain": [],
        }
    audit(db, user, "campaign.link_health_checked", offer.id, result["reason"])
    db.commit()
    return {**result, "ad_platform_approval_verified": False, "creates_clicks": False}
