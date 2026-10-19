import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import (
    String,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
    Index,
    Numeric,
    DateTime,
    Boolean,
    Integer,
    Text,
    CheckConstraint,
    JSON,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base


def identifier():
    return uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    plan: Mapped[str] = mapped_column(String(20), default="trial", server_default="trial")
    subscription_status: Mapped[str] = mapped_column(
        String(20), default="trial", server_default="trial"
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    razorpay_subscription_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    paid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billing_grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_billing_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
        server_default=text("CURRENT_TIMESTAMP"),
        index=True,
    )
    business_type: Mapped[str | None] = mapped_column(String(40))
    usage_types: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    country: Mapped[str | None] = mapped_column(String(2))
    mobile: Mapped[str | None] = mapped_column(String(20))
    teams_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    teams_email: Mapped[str | None] = mapped_column(String(254))
    preferred_language: Mapped[str] = mapped_column(String(5), default="en", server_default="en")
    referral_source: Mapped[str | None] = mapped_column(String(40))
    branding: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("workspace_id", "email"),
        UniqueConstraint("email", name="uq_user_email_global"),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "parent_user_id"], ["users.workspace_id", "users.id"]
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    email: Mapped[str] = mapped_column(String(254))
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    auth_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    parent_user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    consent_version: Mapped[str | None] = mapped_column(String(40))
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    api_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    staff_permissions: Mapped[list | None] = mapped_column(JSON)


class Offer(Base):
    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(["workspace_id", "advertiser_id"], ["users.workspace_id", "users.id"]),
        CheckConstraint("revenue >= 0 AND payout >= 0", name="ck_offer_money"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    destination_url: Mapped[str] = mapped_column(String(2048))
    revenue: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    payout: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    postback_key_hash: Mapped[str] = mapped_column(String(64))
    advertiser_id: Mapped[str | None] = mapped_column(String(32))
    attribution_hours: Mapped[int] = mapped_column(Integer, default=720, server_default="720")
    tracking_rules: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    campaign_model: Mapped[str] = mapped_column(
        String(10), default="legacy", server_default="legacy"
    )
    sale_percentage: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    catalog_settings: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class TrackingLink(Base):
    __tablename__ = "tracking_links"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        ForeignKeyConstraint(["workspace_id", "affiliate_id"], ["users.workspace_id", "users.id"]),
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "offer_id", "affiliate_id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    offer_id: Mapped[str] = mapped_column(String(32))
    affiliate_id: Mapped[str] = mapped_column(String(32))


class Click(Base):
    __tablename__ = "clicks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "link_id"], ["tracking_links.workspace_id", "tracking_links.id"]
        ),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_click_workspace_created", "workspace_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    link_id: Mapped[str] = mapped_column(String(32), index=True)
    sub_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    country: Mapped[str | None] = mapped_column(String(2))
    device: Mapped[str] = mapped_column(String(20), default="unknown", server_default="unknown")
    operating_system: Mapped[str] = mapped_column(
        String(20), default="other", server_default="other"
    )
    language: Mapped[str | None] = mapped_column(String(10))
    ip_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class Conversion(Base):
    __tablename__ = "conversions"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "click_id"], ["clicks.workspace_id", "clicks.id"]),
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        UniqueConstraint(
            "workspace_id",
            "offer_id",
            "event_key",
            "transaction_id",
            name="uq_conversion_event_transaction",
        ),
        UniqueConstraint(
            "workspace_id",
            "click_id",
            "event_key",
            "deduplication_key",
            name="uq_conversion_event_click",
        ),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_conversion_workspace_created", "workspace_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    click_id: Mapped[str] = mapped_column(String(32))
    offer_id: Mapped[str] = mapped_column(String(32))
    transaction_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    revenue: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    payout: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    event_type: Mapped[str | None] = mapped_column(String(10))
    sale_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    event_key: Mapped[str] = mapped_column(String(64), default="initial", server_default="initial")
    deduplication_key: Mapped[str] = mapped_column(String(120), default="", server_default="")
    advertiser_parameters: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    pricing_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "user_id"], ["users.workspace_id", "users.id"]),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ActionToken(Base):
    __tablename__ = "action_tokens"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[str] = mapped_column(String(20))
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "affiliate_id"], ["users.workspace_id", "users.id"]),
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        UniqueConstraint("workspace_id", "affiliate_id", "offer_id", name="uq_callback_campaign"),
        Index(
            "uq_callback_global",
            "workspace_id",
            "affiliate_id",
            unique=True,
            postgresql_where=text("offer_id IS NULL"),
            sqlite_where=text("offer_id IS NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    affiliate_id: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(Text)
    secret: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    offer_id: Mapped[str | None] = mapped_column(String(32))
    method: Mapped[str] = mapped_column(String(4), default="POST", server_default="POST")


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_dispatch", "status", "available_at"),
        ForeignKeyConstraint(["workspace_id", "recipient_id"], ["users.workspace_id", "users.id"]),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(32))
    last_error: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    recipient_id: Mapped[str | None] = mapped_column(String(32), index=True)
    http_status: Mapped[int | None] = mapped_column(Integer)


class BillingEvent(Base):
    __tablename__ = "billing_events"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CommissionEntry(Base):
    __tablename__ = "commission_entries"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "affiliate_id"], ["users.workspace_id", "users.id"]),
        ForeignKeyConstraint(
            ["workspace_id", "conversion_id"], ["conversions.workspace_id", "conversions.id"]
        ),
        UniqueConstraint("workspace_id", "conversion_id", "version"),
        Index("ix_commission_account", "workspace_id", "affiliate_id", "currency"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    affiliate_id: Mapped[str] = mapped_column(String(32))
    conversion_id: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Payout(Base):
    __tablename__ = "payouts"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "affiliate_id"], ["users.workspace_id", "users.id"]),
        UniqueConstraint("workspace_id", "reference"),
        Index("ix_payout_account", "workspace_id", "affiliate_id", "currency"),
        CheckConstraint("amount > 0", name="ck_payout_positive"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    affiliate_id: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    currency: Mapped[str] = mapped_column(String(3))
    reference: Mapped[str] = mapped_column(String(120))
    recorded_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OfferCounter(Base):
    __tablename__ = "offer_counters"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        UniqueConstraint("workspace_id", "offer_id", "period"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(String(32))
    period: Mapped[str] = mapped_column(String(10))
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    conversions: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)


class FraudCounter(Base):
    __tablename__ = "fraud_counters"
    __table_args__ = (UniqueConstraint("workspace_id", "offer_id", "ip_hash", "minute"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(ForeignKey("offers.id"))
    ip_hash: Mapped[str] = mapped_column(String(64))
    minute: Mapped[int] = mapped_column(Integer, index=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class FraudEvent(Base):
    __tablename__ = "fraud_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    offer_id: Mapped[str] = mapped_column(ForeignKey("offers.id"))
    click_id: Mapped[str | None] = mapped_column(ForeignKey("clicks.id"))
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(80))
    score: Mapped[int] = mapped_column(Integer)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class TrackingDomain(Base):
    __tablename__ = "tracking_domains"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(["workspace_id", "advertiser_id"], ["users.workspace_id", "users.id"]),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    hostname: Mapped[str] = mapped_column(String(253), unique=True)
    verification_hash: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    advertiser_id: Mapped[str | None] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(10), default="tracking", server_default="tracking")
    tls_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CampaignApplication(Base):
    __tablename__ = "campaign_applications"
    __table_args__ = (
        UniqueConstraint("workspace_id", "offer_id", "publisher_id"),
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        ForeignKeyConstraint(["workspace_id", "publisher_id"], ["users.workspace_id", "users.id"]),
        Index("ix_application_network_status", "workspace_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(String(32))
    publisher_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    message: Mapped[str] = mapped_column(String(1000), default="")
    review_note: Mapped[str] = mapped_column(String(1000), default="")
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LandingPage(Base):
    __tablename__ = "landing_pages"
    __table_args__ = (
        UniqueConstraint("workspace_id", "domain_id", "slug"),
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        ForeignKeyConstraint(
            ["workspace_id", "domain_id"], ["tracking_domains.workspace_id", "tracking_domains.id"]
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(String(32))
    domain_id: Mapped[str] = mapped_column(String(32))
    slug: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    button_text: Mapped[str] = mapped_column(String(80), default="Continue")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PublisherInvoice(Base):
    __tablename__ = "publisher_invoices"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "publisher_id", "number"),
        ForeignKeyConstraint(["workspace_id", "advertiser_id"], ["users.workspace_id", "users.id"]),
        ForeignKeyConstraint(["workspace_id", "publisher_id"], ["users.workspace_id", "users.id"]),
        CheckConstraint("amount > 0", name="ck_invoice_positive"),
        Index("ix_invoice_advertiser_status", "workspace_id", "advertiser_id", "status"),
        Index("ix_invoice_publisher_status", "workspace_id", "publisher_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    advertiser_id: Mapped[str] = mapped_column(String(32))
    publisher_id: Mapped[str] = mapped_column(String(32))
    number: Mapped[str] = mapped_column(String(80))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20), default="submitted")
    payment_reference: Mapped[str | None] = mapped_column(String(200))
    receipt_url: Mapped[str | None] = mapped_column(String(2048))
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class InvoiceLine(Base):
    __tablename__ = "publisher_invoice_lines"
    __table_args__ = (
        UniqueConstraint("workspace_id", "invoice_id", "conversion_id"),
        ForeignKeyConstraint(
            ["workspace_id", "invoice_id"],
            ["publisher_invoices.workspace_id", "publisher_invoices.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "conversion_id"], ["conversions.workspace_id", "conversions.id"]
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    invoice_id: Mapped[str] = mapped_column(String(32))
    conversion_id: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 4))


class InvoiceReservation(Base):
    __tablename__ = "publisher_invoice_reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "invoice_id"],
            ["publisher_invoices.workspace_id", "publisher_invoices.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "conversion_id"], ["conversions.workspace_id", "conversions.id"]
        ),
    )
    conversion_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    invoice_id: Mapped[str] = mapped_column(String(32))


class InvoiceEvent(Base):
    __tablename__ = "publisher_invoice_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "invoice_id"],
            ["publisher_invoices.workspace_id", "publisher_invoices.id"],
        ),
        ForeignKeyConstraint(["workspace_id", "actor_id"], ["users.workspace_id", "users.id"]),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    invoice_id: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(30))
    detail: Mapped[str] = mapped_column(String(1000), default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PayoutProvider(Base):
    __tablename__ = "payout_providers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), unique=True)
    credentials: Mapped[str] = mapped_column(Text)
    automatic: Mapped[bool] = mapped_column(Boolean, default=False)
    minimum_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=1000)
    interval_hours: Mapped[int] = mapped_column(Integer, default=168)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    configured_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    beneficiary_cursor: Mapped[str] = mapped_column(String(32), default="", server_default="")


class PayoutRequest(Base):
    __tablename__ = "payout_requests"
    __table_args__ = (
        UniqueConstraint("workspace_id", "request_key"),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(["workspace_id", "affiliate_id"], ["users.workspace_id", "users.id"]),
        Index("ix_payout_request_account", "workspace_id", "affiliate_id", "currency", "status"),
        CheckConstraint("amount > 0", name="ck_payout_request_positive"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    affiliate_id: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    request_key: Mapped[str] = mapped_column(String(80))
    fund_account_id: Mapped[str] = mapped_column(String(100))
    provider_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    outbox_id: Mapped[str | None] = mapped_column(ForeignKey("outbox_events.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    requested_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))


class PayoutBeneficiary(Base):
    __tablename__ = "payout_beneficiaries"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "affiliate_id"], ["users.workspace_id", "users.id"]),
        UniqueConstraint("workspace_id", "affiliate_id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    affiliate_id: Mapped[str] = mapped_column(String(32))
    fund_account_id: Mapped[str] = mapped_column(String(100))


class PayoutAdjustment(Base):
    __tablename__ = "payout_adjustments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "affiliate_id"],
            ["users.workspace_id", "users.id"],
            name="fk_adjustment_recipient",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "request_id"],
            ["payout_requests.workspace_id", "payout_requests.id"],
            name="fk_adjustment_request",
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    affiliate_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[str] = mapped_column(ForeignKey("payout_requests.id"), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
