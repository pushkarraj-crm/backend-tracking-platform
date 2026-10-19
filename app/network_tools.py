"""Bounded operator tools: imports, previews, health checks, smart links and automation."""

import csv
import io
import ipaddress
import secrets
import socket
import ssl
import http.client
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from urllib.parse import urlsplit, urljoin, urlencode
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import Field, ValidationError, model_validator
from typing import Literal
from sqlalchemy import select, func
from .schemas import StrictModel, ActiveUpdate
from .security import DB, Actor
from .models import (
    User,
    Offer,
    TrackingLink,
    Click,
    Conversion,
    InvoiceReservation,
    Workspace,
    OfferCounter,
    CampaignApplication,
)
from .network_models import SmartLink, AutomationRule, AutomationLog
from .campaigns import CampaignBody, create_campaign_record, campaign_output, manage_campaign
from .campaign_policy import safe_url, parameters, destination, can_discover, TRACKED_PARAMETERS
from .services import audit, entitlement, utc
from .routes import owned, visible_users
from .network_account import notify, notify_admins

router = APIRouter(tags=["Network tools"])


def employee_capability(user, permission):
    if (
        user.role == "staff"
        and user.staff_permissions is not None
        and permission not in user.staff_permissions
    ):
        raise HTTPException(403, "Employee permission is required for this operation")


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
    allowed = set(CampaignBody.model_fields)
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
    from .config import get_settings

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
    from .conversion_engine import transition

    for row in rows:
        transition(db, user, row, body.status)
    db.commit()
    return {"updated": len(rows), "status": body.status}


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
        from .callbacks import output

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
    from .campaign_policy import TOKEN
    from urllib.parse import unquote

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


class SmartChild(StrictModel):
    link_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    weight: int = Field(default=1, ge=1, le=10000)


class SmartBody(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    publisher_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    children: list[SmartChild] = Field(min_length=2, max_length=50)

    @model_validator(mode="after")
    def valid(self):
        if len({r.link_id for r in self.children}) != len(self.children):
            raise ValueError("Select unique child links")
        return self


@router.post("/smart-links", status_code=201)
def create_smart(body: SmartBody, db: DB, user: Actor):
    publisher = owned(db, User, body.publisher_id or user.id, user)
    if publisher.role != "publisher" or not publisher.active:
        raise HTTPException(422, "Select an active publisher")
    if (
        user.role not in {"admin", "staff"}
        and publisher.id != user.id
        and not (user.role == "affiliate" and publisher.parent_user_id == user.id)
    ):
        raise HTTPException(403, "Can create smart links only for your own publishers")
    links = list(
        db.execute(
            select(TrackingLink, Offer)
            .join(Offer, Offer.id == TrackingLink.offer_id)
            .where(
                TrackingLink.workspace_id == user.workspace_id,
                TrackingLink.affiliate_id == publisher.id,
                TrackingLink.id.in_([c.link_id for c in body.children]),
            )
        )
    )
    if len(links) != len(body.children) or len({o.advertiser_id for _, o in links}) != 1:
        raise HTTPException(
            422, "Child links must belong to this publisher and a single advertiser"
        )
    approved = set(
        db.scalars(
            select(CampaignApplication.offer_id).where(
                CampaignApplication.workspace_id == user.workspace_id,
                CampaignApplication.publisher_id == publisher.id,
                CampaignApplication.status == "approved",
            )
        )
    )
    if any(o.id not in approved or not o.active for _, o in links):
        raise HTTPException(403, "All child campaigns require active approved publisher access")
    db.scalar(select(User).where(User.id == publisher.id).with_for_update())
    if (
        db.scalar(
            select(func.count())
            .select_from(SmartLink)
            .where(SmartLink.publisher_id == publisher.id)
        )
        >= 100
    ):
        raise HTTPException(409, "Smart link limit reached")
    row = SmartLink(
        workspace_id=user.workspace_id,
        publisher_id=publisher.id,
        name=body.name,
        children=[c.model_dump() for c in body.children],
    )
    db.add(row)
    db.flush()
    audit(db, user, "smart_link.created", row.id)
    db.commit()
    return smart_output(db, user, row)


def smart_output(db, user, row):
    from .domains import link_base

    return {
        "id": row.id,
        "name": row.name,
        "publisher_id": row.publisher_id,
        "children": row.children,
        "active": row.active,
        "url": link_base(db, user, None) + "/s/" + row.id,
    }


@router.get("/smart-links")
def smart_links(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    query = select(SmartLink).where(SmartLink.workspace_id == user.workspace_id)
    if user.role in {"affiliate", "publisher"}:
        query = query.where(SmartLink.publisher_id.in_(visible_users(user)))
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Publisher or network staff required")
    return [
        smart_output(db, user, row)
        for row in db.scalars(query.order_by(SmartLink.id).offset(offset).limit(limit))
    ]


@router.patch("/smart-links/{smart_id}")
def smart_status(smart_id: str, body: ActiveUpdate, db: DB, user: Actor):
    row = owned(db, SmartLink, smart_id, user)
    if user.role not in {"admin", "staff"} and row.publisher_id not in list(
        db.scalars(visible_users(user))
    ):
        raise HTTPException(404, "Smart link not found")
    row.active = body.active
    audit(db, user, "smart_link.active", row.id, str(body.active))
    db.commit()
    return smart_output(db, user, row)


@router.get("/s/{smart_id}", include_in_schema=False)
def smart_redirect(smart_id: str, request: Request, db: DB):
    from .tracking import Rules, context
    from .domains import enforce_tracking_host

    row = db.get(SmartLink, smart_id)
    if not row or not row.active:
        raise HTTPException(404, "Smart link not found")
    publisher = db.get(User, row.publisher_id)
    entitlement(db.get(Workspace, row.workspace_id))
    if not publisher.active or (
        publisher.parent_user_id and not db.get(User, publisher.parent_user_id).active
    ):
        raise HTTPException(410, "Publisher disabled")
    weights = {c["link_id"]: c["weight"] for c in row.children}
    offers = list(
        db.execute(
            select(TrackingLink, Offer)
            .join(Offer, Offer.id == TrackingLink.offer_id)
            .where(
                TrackingLink.workspace_id == row.workspace_id,
                TrackingLink.affiliate_id == row.publisher_id,
                TrackingLink.id.in_(weights),
                Offer.active.is_(True),
            )
        )
    )
    approved = set(
        db.scalars(
            select(CampaignApplication.offer_id).where(
                CampaignApplication.workspace_id == row.workspace_id,
                CampaignApplication.publisher_id == row.publisher_id,
                CampaignApplication.status == "approved",
            )
        )
    )
    metadata, _, _ = context(request)
    now = datetime.now(timezone.utc)
    eligible = []
    counters = {
        (c.offer_id, c.period): c
        for c in db.scalars(
            select(OfferCounter).where(
                OfferCounter.workspace_id == row.workspace_id,
                OfferCounter.offer_id.in_([o.id for _, o in offers]),
                OfferCounter.period.in_(["total", now.date().isoformat()]),
            )
        )
    }
    for link, offer in offers:
        if offer.id not in approved:
            continue
        owner = db.get(User, offer.advertiser_id) if offer.advertiser_id else None
        if owner and not owner.active:
            continue
        rules = Rules.model_validate(offer.tracking_rules)
        if (
            (rules.starts_at and now < rules.starts_at)
            or (rules.ends_at and now >= rules.ends_at)
            or (rules.utc_hours and now.hour not in rules.utc_hours)
        ):
            continue
        if any(
            values and metadata[key] not in values
            for key, values in [
                ("country", rules.countries),
                ("device", rules.devices),
                ("operating_system", rules.operating_systems),
                ("language", rules.languages),
            ]
        ):
            continue
        limited = False
        for period, limit_value in [
            ("total", rules.total_clicks),
            (now.date().isoformat(), rules.daily_clicks),
        ]:
            counter = counters.get((offer.id, period))
            if limit_value is not None and counter and counter.clicks >= limit_value:
                limited = True
        if not limited:
            eligible.append((link, offer))
    if not eligible:
        raise HTTPException(410, "No eligible smart-link campaign is available")
    enforce_tracking_host(db, request, row.workspace_id, eligible[0][1].advertiser_id)
    choice = secrets.randbelow(sum(weights[child.id] for child, _ in eligible))
    selected = eligible[-1][0]
    for link, _ in eligible:
        choice -= weights[link.id]
        if choice < 0:
            selected = link
            break
    from .config import get_settings
    from .tenancy import company_context

    company = company_context.get()
    base = get_settings().public_base_url.rstrip("/") + ("/c/" + company.slug if company else "")
    query = {
        k: v
        for k, v in request.query_params.items()
        if k in TRACKED_PARAMETERS | {"sub_id", "url"} and len(v) <= 2048
    }
    return RedirectResponse(
        base + "/t/" + selected.id + ("?" + urlencode(query) if query else ""), status_code=302
    )


class AutomationBody(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    offer_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    lookback_hours: int = Field(default=24, ge=1, le=168)
    minimum_clicks: int = Field(default=100, ge=1, le=10000000)
    metric: Literal["clicks", "approved_conversions", "conversion_rate"] = "conversion_rate"
    comparison: Literal["lt", "gt"] = "lt"
    threshold: Decimal = Field(ge=0, max_digits=14, decimal_places=4)
    action: Literal["notify", "pause"] = "notify"
    cooldown_hours: int = Field(default=24, ge=1, le=168)


@router.post("/tools/automation", status_code=201)
def create_automation(body: AutomationBody, db: DB, user: Actor):
    manage_campaign(db, user, body.offer_id, lock=True)
    employee_capability(user, "campaigns:write")
    if (
        db.scalar(
            select(func.count())
            .select_from(AutomationRule)
            .where(
                AutomationRule.workspace_id == user.workspace_id,
                AutomationRule.offer_id == body.offer_id,
            )
        )
        >= 20
    ):
        raise HTTPException(409, "Automation limit reached")
    row = AutomationRule(
        workspace_id=user.workspace_id,
        offer_id=body.offer_id,
        user_id=user.id,
        name=body.name,
        configuration=body.model_dump(mode="json", exclude={"name", "offer_id"}),
    )
    db.add(row)
    db.flush()
    audit(db, user, "automation.created", row.id)
    db.commit()
    return automation_output(row)


def automation_output(row):
    return {
        "id": row.id,
        "name": row.name,
        "offer_id": row.offer_id,
        "active": row.active,
        "next_check_at": row.next_check_at,
        "last_triggered_at": row.last_triggered_at,
        **row.configuration,
    }


@router.get("/tools/automation")
def automations(db: DB, user: Actor):
    query = (
        select(AutomationRule)
        .join(Offer, Offer.id == AutomationRule.offer_id)
        .where(AutomationRule.workspace_id == user.workspace_id)
    )
    if user.role == "advertiser":
        query = query.where(Offer.advertiser_id == user.id)
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Campaign management role required")
    return [automation_output(r) for r in db.scalars(query.order_by(AutomationRule.id).limit(100))]


@router.patch("/tools/automation/{rule_id}")
def automation_active(rule_id: str, body: ActiveUpdate, db: DB, user: Actor):
    row = owned(db, AutomationRule, rule_id, user)
    manage_campaign(db, user, row.offer_id)
    employee_capability(user, "campaigns:write")
    row.active = body.active
    audit(db, user, "automation.active", row.id, str(body.active))
    db.commit()
    return automation_output(row)


@router.get("/tools/automation-logs")
def automation_logs(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    query = (
        select(AutomationLog)
        .join(AutomationRule, AutomationRule.id == AutomationLog.rule_id)
        .join(Offer, Offer.id == AutomationRule.offer_id)
        .where(AutomationLog.workspace_id == user.workspace_id)
    )
    if user.role == "advertiser":
        query = query.where(Offer.advertiser_id == user.id)
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Campaign management role required")
    return [
        {k: getattr(r, k) for k in ["id", "rule_id", "action", "observed", "created_at"]}
        for r in db.scalars(
            query.order_by(AutomationLog.created_at.desc(), AutomationLog.id)
            .offset(offset)
            .limit(limit)
        )
    ]


def run_automations(db):
    at = datetime.now(timezone.utc)
    rows = list(
        db.scalars(
            select(AutomationRule)
            .where(AutomationRule.active.is_(True), AutomationRule.next_check_at <= at)
            .order_by(AutomationRule.next_check_at, AutomationRule.id)
            .with_for_update(skip_locked=True)
            .limit(100)
        )
    )
    for row in rows:
        row.next_check_at = at + timedelta(hours=1)
        config = AutomationBody(name=row.name, offer_id=row.offer_id, **row.configuration)
        if row.last_triggered_at and utc(row.last_triggered_at) > at - timedelta(
            hours=config.cooldown_hours
        ):
            continue
        offer = db.scalar(
            select(Offer)
            .where(Offer.id == row.offer_id, Offer.workspace_id == row.workspace_id)
            .with_for_update()
        )
        creator = db.get(User, row.user_id)
        workspace = db.get(Workspace, row.workspace_id)
        if not creator or not creator.active or not workspace.active or not offer.active:
            continue
        if (
            creator.role == "staff"
            and creator.staff_permissions is not None
            and "campaigns:write" not in creator.staff_permissions
        ):
            continue
        try:
            entitlement(workspace)
        except HTTPException:
            continue
        start = at - timedelta(hours=config.lookback_hours)
        clicks = db.scalar(
            select(func.count(Click.id))
            .join(TrackingLink, TrackingLink.id == Click.link_id)
            .where(
                Click.workspace_id == row.workspace_id,
                TrackingLink.offer_id == offer.id,
                Click.created_at >= start,
                Click.created_at < at,
            )
        )
        if clicks < config.minimum_clicks:
            continue
        conversions = db.scalar(
            select(func.count(Conversion.id)).where(
                Conversion.workspace_id == row.workspace_id,
                Conversion.offer_id == offer.id,
                Conversion.status == "approved",
                Conversion.created_at >= start,
                Conversion.created_at < at,
            )
        )
        unique = db.scalar(
            select(func.count(func.distinct(Conversion.click_id)))
            .join(Click, Click.id == Conversion.click_id)
            .where(
                Conversion.workspace_id == row.workspace_id,
                Conversion.offer_id == offer.id,
                Conversion.status == "approved",
                Click.created_at >= start,
                Click.created_at < at,
            )
        )
        values = {
            "clicks": Decimal(clicks),
            "approved_conversions": Decimal(conversions),
            "conversion_rate": Decimal(unique) * 100 / Decimal(clicks),
        }
        value = values[config.metric]
        triggered = (
            value < config.threshold if config.comparison == "lt" else value > config.threshold
        )
        if not triggered:
            continue
        if config.action == "pause":
            offer.active = False
        row.last_triggered_at = at
        db.add(
            AutomationLog(
                workspace_id=row.workspace_id,
                rule_id=row.id,
                action=config.action,
                observed={k: str(v) for k, v in values.items()},
            )
        )
        audit(db, creator, "automation.triggered", offer.id, config.action)
        notify(
            db,
            row.workspace_id,
            [row.user_id, offer.advertiser_id],
            "automation.triggered",
            "Campaign automation triggered",
            row.name,
            offer.id,
        )
        notify_admins(
            db,
            row.workspace_id,
            "automation.triggered",
            "Campaign automation triggered",
            row.name,
            offer.id,
        )
