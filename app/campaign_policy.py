"""Typed campaign configuration, bounded rule evaluation and safe tracking templates."""

import ipaddress
import re
import secrets
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from fastapi import HTTPException
from pydantic import Field, HttpUrl, model_validator
from sqlalchemy import select
from .schemas import StrictModel
from .models import CampaignApplication

TRACKED_PARAMETERS = {
    "source",
    "sub_aff_id",
    "aff_click_id",
    *[f"aff_sub{i}" for i in range(1, 11)],
    "gclid",
    "gbraid",
    "wbraid",
    "fbclid",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
}
MACROS = {
    "tid",
    "click_id",
    "aff_id",
    "affiliate_id",
    "offerid",
    "campaign_id",
    "offername",
    "adv_id",
    "country",
    "device",
    "os",
    "timestamp",
    "sub_id",
    *TRACKED_PARAMETERS,
}
TOKEN = re.compile(r"\{([^{}]+)\}")


def hostname(value):
    from .domains import DomainBody

    return DomainBody.canonical(value)


def safe_url(value, templates=False):
    text = str(value)
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValueError("Invalid HTTPS URL") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or port not in {None, 443}
        or len(text) > 2048
    ):
        raise ValueError("Use HTTPS port 443 without credentials; maximum URL length is 2048")
    if any(c in parts.netloc for c in "{}") or "%7b" in parts.netloc.lower():
        raise ValueError("Tracking macros cannot change the hostname")
    if templates:
        from urllib.parse import unquote

        decoded = unquote(parts.path + "?" + parts.query)
        if "{" in TOKEN.sub("", decoded) or "}" in TOKEN.sub("", decoded):
            raise ValueError("Unbalanced tracking macro")
        if set(TOKEN.findall(decoded)) - MACROS:
            raise ValueError("Unknown tracking macro")
        if any(TOKEN.search(unquote(k)) for k, _ in parse_qsl(parts.query)):
            raise ValueError("Place tracking macros in query values only")
        if TOKEN.search(unquote(parts.path)):
            raise ValueError("Place tracking macros in query values, not paths")
    return text


class CatalogSettings(StrictModel):
    cap_alert_percent: int = Field(default=80, ge=1, le=100)
    visibility: Literal["open", "approval", "private"] = "approval"
    categories: list[str] = Field(default_factory=list, max_length=8)
    preview_url: HttpUrl | None = None
    terms: str = Field(default="", max_length=20000)
    internal_notes: str = Field(default="", max_length=10000)
    external_id: str = Field(default="", max_length=120)
    deep_link_enabled: bool = False
    deep_link_hosts: list[str] = Field(default_factory=list, max_length=20)
    postback_ip_ranges: list[str] = Field(default_factory=list, max_length=100)
    initial_repeatable: bool = False
    initial_auto_approve: bool = False
    fallback_url: HttpUrl | None = None

    @model_validator(mode="after")
    def valid(self):
        if any(not name.strip() or len(name) > 80 for name in self.categories):
            raise ValueError("Categories must contain 1–80 characters")
        self.categories = sorted(set(name.strip() for name in self.categories))
        self.deep_link_hosts = sorted(set(hostname(v) for v in self.deep_link_hosts))
        self.postback_ip_ranges = [
            str(ipaddress.ip_network(v, strict=False)) for v in self.postback_ip_ranges
        ]
        for url in [self.preview_url, self.fallback_url]:
            if url:
                safe_url(url)
        return self


class ProfileBody(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10000)
    destination_url: HttpUrl
    settings: CatalogSettings = Field(default_factory=CatalogSettings)

    @model_validator(mode="after")
    def valid(self):
        safe_url(self.destination_url, templates=True)
        if self.settings.fallback_url and urlsplit(
            str(self.settings.fallback_url)
        ).hostname not in {
            urlsplit(str(self.destination_url)).hostname,
            *self.settings.deep_link_hosts,
        }:
            raise ValueError(
                "Fallback must use the advertiser destination or an explicitly permitted host"
            )
        return self


class EventBody(StrictModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    model: Literal["CPL", "CPS"] = "CPL"
    payout: Decimal = Field(default=0, ge=0, max_digits=14, decimal_places=4)
    revenue: Decimal = Field(default=0, ge=0, max_digits=14, decimal_places=4)
    sale_percentage: Decimal | None = Field(
        default=None, gt=0, le=100, max_digits=7, decimal_places=4
    )
    repeatable: bool = False
    auto_approve: bool = False
    active: bool = True

    @model_validator(mode="after")
    def valid(self):
        if self.code == "initial":
            raise ValueError("The initial event is configured in the campaign profile")
        if (self.model == "CPS") != (self.sale_percentage is not None):
            raise ValueError("CPS requires a percentage; CPL does not accept one")
        return self


CONDITION_FIELDS = {
    "country",
    "device",
    "operating_system",
    "language",
    "source",
    "sub_id",
    "sub_aff_id",
    "aff_click_id",
    "event",
    "publisher_id",
    "sale_amount",
    "ctit",
    "weekday",
    *[f"aff_sub{i}" for i in range(1, 11)],
    *[f"adv_sub{i}" for i in range(1, 6)],
}


class Condition(StrictModel):
    field: str = Field(max_length=32)
    operator: Literal[
        "eq",
        "ne",
        "in",
        "not_in",
        "contains",
        "not_contains",
        "is_empty",
        "not_empty",
        "gt",
        "lt",
        "between",
    ] = "eq"
    value: str | list[str] = ""

    @model_validator(mode="after")
    def valid(self):
        if self.field not in CONDITION_FIELDS:
            raise ValueError("Unsupported condition field")
        values = [self.value] if isinstance(self.value, str) else self.value
        if len(values) > 50 or any(len(v) > 256 for v in values):
            raise ValueError("Condition values are too large")
        if self.operator in {"in", "not_in", "between"} and not isinstance(self.value, list):
            raise ValueError("This operator requires a list")
        if self.operator not in {"in", "not_in", "between"} and not isinstance(self.value, str):
            raise ValueError("This operator requires a single value")
        if self.operator == "between" and len(values) != 2:
            raise ValueError("Between requires two values")
        if self.operator in {"gt", "lt", "between"}:
            try:
                if any(not Decimal(v).is_finite() for v in values):
                    raise ValueError("Use finite numeric values")
            except InvalidOperation:
                raise ValueError("Use numeric values") from None
        return self


class Conditions(StrictModel):
    all: list[Condition] = Field(default_factory=list, max_length=20)


def matches(conditions, values):
    for condition in Conditions.model_validate(conditions).all:
        value = values.get(condition.field)
        actual = "" if value is None else str(value)
        expected = condition.value
        operation = condition.operator
        if operation == "eq":
            ok = actual == expected
        elif operation == "ne":
            ok = actual != expected
        elif operation == "in":
            ok = actual in expected
        elif operation == "not_in":
            ok = actual not in expected
        elif operation == "contains":
            ok = isinstance(expected, str) and expected in actual
        elif operation == "not_contains":
            ok = isinstance(expected, str) and expected not in actual
        elif operation == "is_empty":
            ok = not actual
        elif operation == "not_empty":
            ok = bool(actual)
        else:
            try:
                number = Decimal(actual)
                if not number.is_finite():
                    return False
                if operation == "gt":
                    ok = number > Decimal(expected)
                elif operation == "lt":
                    ok = number < Decimal(expected)
                else:
                    ok = Decimal(expected[0]) <= number <= Decimal(expected[1])
            except (InvalidOperation, TypeError):
                return False
        if not ok:
            return False
    return True


def policy(offer):
    return CatalogSettings.model_validate(offer.catalog_settings or {})


def can_discover(db, user, offer):
    if user.workspace_id != offer.workspace_id:
        return False
    if user.role in {"admin", "staff"}:
        return True
    if user.role == "advertiser":
        return offer.advertiser_id == user.id
    if user.role not in {"publisher", "affiliate"}:
        return False
    if not offer.active:
        return False
    if policy(offer).visibility != "private":
        return True
    return bool(
        db.scalar(
            select(CampaignApplication.id).where(
                CampaignApplication.workspace_id == user.workspace_id,
                CampaignApplication.offer_id == offer.id,
                CampaignApplication.publisher_id == user.id,
                CampaignApplication.status == "approved",
            )
        )
    )


def public_settings(offer, user):
    value = policy(offer).model_dump(mode="json")
    if user.role in {"publisher", "affiliate"}:
        for key in [
            "internal_notes",
            "postback_ip_ranges",
            "initial_auto_approve",
            "initial_repeatable",
            "fallback_url",
            "external_id",
        ]:
            value.pop(key, None)
    return value


def parameters(query):
    result = {}
    for key in TRACKED_PARAMETERS:
        values = (
            query.getlist(key)
            if hasattr(query, "getlist")
            else ([query[key]] if key in query else [])
        )
        if len(values) > 1:
            raise HTTPException(422, "Duplicate tracking parameter: " + key)
        if values:
            value = str(values[0])
            if len(value) > 256 or any(ord(c) < 32 for c in value):
                raise HTTPException(422, "Invalid tracking parameter: " + key)
            result[key] = value
    return result


def destination(db, offer, link, click, requested_url=None, destination_id=None):
    from .network_models import CampaignDestination

    settings = policy(offer)
    url = offer.destination_url
    variants = list(
        db.scalars(
            select(CampaignDestination)
            .where(
                CampaignDestination.workspace_id == offer.workspace_id,
                CampaignDestination.offer_id == offer.id,
                CampaignDestination.active.is_(True),
            )
            .order_by(CampaignDestination.id)
            .limit(100)
        )
    )
    variants = [
        v
        for v in variants
        if (not v.countries or click.country in v.countries)
        and (not v.publisher_ids or link.affiliate_id in v.publisher_ids)
    ]
    if destination_id:
        variants = [v for v in variants if v.id == destination_id]
        if not variants:
            raise HTTPException(422, "Landing variant is unavailable for this publisher/traffic")
    if variants:
        choice = secrets.randbelow(sum(v.weight for v in variants))
        for variant in variants:
            choice -= variant.weight
            if choice < 0:
                url = variant.url
                click.attributes = {**click.attributes, "destination_id": variant.id}
                break
    if requested_url:
        try:
            safe_url(requested_url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        allowed = set(settings.deep_link_hosts) or {urlsplit(offer.destination_url).hostname}
        if not settings.deep_link_enabled or urlsplit(requested_url).hostname not in allowed:
            raise HTTPException(422, "Deep link host is not permitted for this campaign")
        url = requested_url
    parts = urlsplit(url)
    values = {
        **click.attributes,
        "tid": click.id,
        "click_id": click.id,
        "aff_id": link.affiliate_id,
        "affiliate_id": link.affiliate_id,
        "offerid": offer.id,
        "campaign_id": offer.id,
        "offername": offer.name,
        "adv_id": offer.advertiser_id or "",
        "country": click.country or "",
        "device": click.device,
        "os": click.operating_system,
        "timestamp": str(int(click.created_at.timestamp())),
        "sub_id": click.sub_id or "",
    }
    query = [
        (key, TOKEN.sub(lambda m: str(values.get(m[1], "")), value))
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "click_id"
    ]
    incoming = {k: v for k, v in click.attributes.items() if k in TRACKED_PARAMETERS}
    query = [(k, v) for k, v in query if k not in incoming]
    query.extend(incoming.items())
    query.append(("click_id", click.id))
    return urlunsplit(parts._replace(query=urlencode(query)))


def rule_values(click, publisher_id, event="initial", sale_amount=None, advertiser_parameters=None):
    from datetime import datetime, timezone
    from .services import utc

    return {
        **(click.attributes or {}),
        **(advertiser_parameters or {}),
        "country": click.country or "",
        "device": click.device,
        "operating_system": click.operating_system,
        "language": click.language or "",
        "sub_id": click.sub_id or "",
        "publisher_id": publisher_id,
        "event": event,
        "sale_amount": str(sale_amount) if sale_amount is not None else "",
        "ctit": str(int((datetime.now(timezone.utc) - utc(click.created_at)).total_seconds())),
        "weekday": str(utc(click.created_at).weekday()),
    }
