"""Publisher global/campaign callbacks with bounded query templates and signed tests."""

import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, HttpUrl, model_validator
from sqlalchemy import select
from .security import DB, Actor
from .schemas import StrictModel, ActiveUpdate
from .models import User, Offer, WebhookEndpoint, CampaignApplication, OutboxEvent
from .routes import owned, entitled
from .services import audit, encrypt, decrypt, enqueue
from .campaign_policy import TOKEN
from .delivery import validate_destination

router = APIRouter(tags=["Partner callbacks"])
CALLBACK_MACROS = {
    "tid",
    "click_id",
    "conversion_id",
    "campaign_id",
    "transaction_id",
    "payout",
    "currency",
    "status",
    "event_key",
    "event_token",
    "source",
    "aff_click_id",
    "sub_aff_id",
    "version",
    *[f"aff_sub{i}" for i in range(1, 11)],
    *[f"adv_sub{i}" for i in range(1, 6)],
}


def stored_url(endpoint):
    return decrypt(endpoint.url[7:]) if endpoint.url.startswith("enc:v1:") else endpoint.url


def store_url(value):
    return "enc:v1:" + encrypt(value)


def render_url(url, event):
    parts = urlsplit(url)
    values = {
        **event.get("tracking", {}),
        **event.get("advertiser_parameters", {}),
        **{k: v for k, v in event.items() if k not in {"tracking", "advertiser_parameters"}},
        "tid": event.get("click_id", ""),
        "event_token": event.get("event_key", ""),
    }
    query = [
        (k, TOKEN.sub(lambda m: str(values.get(m[1], "") or ""), v))
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(query)))


class CallbackBody(StrictModel):
    publisher_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    offer_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    url: HttpUrl
    method: str = Field(default="POST", pattern=r"^(GET|POST)$")

    @model_validator(mode="after")
    def valid(self):
        from urllib.parse import unquote
        import re

        text = str(self.url)
        parts = urlsplit(text)
        if (
            len(text) > 2048
            or parts.username
            or parts.password
            or parts.scheme != "https"
            or parts.port not in {None, 443}
        ):
            raise ValueError("Use a bounded HTTPS callback URL without credentials")
        if "{" in unquote(parts.netloc + parts.path) or "}" in unquote(parts.netloc + parts.path):
            raise ValueError("Macros are allowed only in callback query values")
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if "{" in key or "}" in key:
                raise ValueError("Query keys cannot contain macros")
            tokens = set(re.findall(r"\{([^}]+)\}", value))
            if tokens - CALLBACK_MACROS:
                raise ValueError("Unsupported callback macro")
        return self


def owner_endpoint(db, user, endpoint_id):
    endpoint = owned(db, WebhookEndpoint, endpoint_id, user)
    if user.role not in {"admin", "staff"} and endpoint.affiliate_id != user.id:
        raise HTTPException(404, "Callback not found")
    return endpoint


def output(endpoint):
    parts = urlsplit(stored_url(endpoint))
    sensitive = {
        "key",
        "api_key",
        "token",
        "secret",
        "password",
        "access_token",
        "auth",
        "authorization",
    }
    url = urlunsplit(
        parts._replace(
            query=urlencode(
                [
                    (k, "[REDACTED]" if k.lower() in sensitive else v)
                    for k, v in parse_qsl(parts.query, keep_blank_values=True)
                ]
            )
        )
    )
    return {
        "id": endpoint.id,
        "publisher_id": endpoint.affiliate_id,
        "offer_id": endpoint.offer_id,
        "url": url,
        "method": endpoint.method,
        "active": endpoint.active,
    }


@router.get("/callbacks")
def callbacks(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    query = select(WebhookEndpoint).where(WebhookEndpoint.workspace_id == user.workspace_id)
    if user.role in {"publisher", "affiliate"}:
        query = query.where(WebhookEndpoint.affiliate_id == user.id)
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Publisher or staff access required")
    return [
        output(e)
        for e in db.scalars(query.order_by(WebhookEndpoint.id).offset(offset).limit(limit))
    ]


@router.put("/callbacks")
def save_callback(body: CallbackBody, db: DB, user: Actor):
    entitled(db, user)
    publisher = owned(db, User, body.publisher_id or user.id, user)
    if publisher.role not in {"publisher", "affiliate"} or not publisher.active:
        raise HTTPException(422, "Select an active publisher or affiliate")
    if user.role not in {"admin", "staff"} and user.id != publisher.id:
        raise HTTPException(403, "Can configure only your own callbacks")
    if body.offer_id:
        offer = owned(db, Offer, body.offer_id, user)
        application = db.scalar(
            select(CampaignApplication.id).where(
                CampaignApplication.workspace_id == user.workspace_id,
                CampaignApplication.offer_id == offer.id,
                CampaignApplication.publisher_id == publisher.id,
                CampaignApplication.status == "approved",
            )
        )
        if publisher.role == "publisher" and not application:
            raise HTTPException(403, "Campaign callback requires approved publisher access")
    preview = render_url(str(body.url), {key: "preview" for key in CALLBACK_MACROS})
    validate_destination(preview)
    # Serialize scope uniqueness even when the caller saves two global callbacks at once.
    db.scalar(select(User).where(User.id == publisher.id).with_for_update())
    row = db.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.workspace_id == user.workspace_id,
            WebhookEndpoint.affiliate_id == publisher.id,
            WebhookEndpoint.offer_id == body.offer_id,
        )
    )
    raw = secrets.token_urlsafe(48)
    if not row:
        row = WebhookEndpoint(
            workspace_id=user.workspace_id, affiliate_id=publisher.id, offer_id=body.offer_id
        )
        db.add(row)
    row.url = store_url(str(body.url))
    row.method = body.method
    row.secret = encrypt(raw)
    row.active = True
    db.flush()
    audit(db, user, "callback.configured", row.id)
    db.commit()
    return {**output(row), "secret": raw}


@router.patch("/callbacks/{endpoint_id}")
def callback_status(endpoint_id: str, body: ActiveUpdate, db: DB, user: Actor):
    row = owner_endpoint(db, user, endpoint_id)
    row.active = body.active
    audit(db, user, "callback.active", row.id, str(body.active))
    db.commit()
    return output(row)


@router.post("/callbacks/{endpoint_id}/test", status_code=202)
def test_callback(endpoint_id: str, db: DB, user: Actor):
    row = owner_endpoint(db, user, endpoint_id)
    if not row.active:
        raise HTTPException(409, "Callback is disabled")
    test = {
        "is_test": True,
        "conversion_id": "TEST_ONLY",
        "click_id": "TEST_ONLY",
        "campaign_id": row.offer_id or "GLOBAL_TEST",
        "transaction_id": "INTEGRATION_TEST",
        "status": "test",
        "payout": "0.0000",
        "currency": "USD",
        "event_key": "integration_test",
        "version": 0,
        "tracking": {},
        "advertiser_parameters": {},
    }
    validate_destination(render_url(stored_url(row), test))
    event = enqueue(
        db,
        user.workspace_id,
        "webhook",
        {"endpoint_id": row.id, "event": test},
        recipient_id=row.affiliate_id,
    )
    db.flush()
    audit(db, user, "callback.test_queued", row.id)
    db.commit()
    return {"id": event.id, "is_test": True, "financial_records_created": False, "status": "queued"}


@router.get("/callback-deliveries")
def deliveries(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    query = select(OutboxEvent).where(
        OutboxEvent.workspace_id == user.workspace_id, OutboxEvent.kind == "webhook"
    )
    if user.role in {"publisher", "affiliate"}:
        query = query.where(OutboxEvent.recipient_id == user.id)
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Publisher or staff access required")
    return [
        {
            k: getattr(e, k)
            for k in ["id", "status", "attempts", "http_status", "last_error", "created_at"]
        }
        for e in db.scalars(
            query.order_by(OutboxEvent.created_at.desc(), OutboxEvent.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@router.post("/callback-deliveries/{event_id}/retry", status_code=202)
def retry(event_id: str, db: DB, user: Actor):
    event = owned(db, OutboxEvent, event_id, user)
    if event.kind != "webhook" or (
        user.role not in {"admin", "staff"} and event.recipient_id != user.id
    ):
        raise HTTPException(404, "Callback delivery not found")
    if event.status != "failed":
        raise HTTPException(409, "Only failed deliveries can be retried")
    from .models import now

    event.status = "pending"
    event.attempts = 0
    event.available_at = now()
    audit(db, user, "callback.delivery_retried", event.id)
    db.commit()
    return {"queued": True}
