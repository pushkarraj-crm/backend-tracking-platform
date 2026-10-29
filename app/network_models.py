"""Relational network operations; all references are scoped by workspace."""

from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import (
    String,
    Text,
    Boolean,
    Integer,
    Numeric,
    DateTime,
    Date,
    JSON,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
    Index,
    CheckConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .models import identifier, now


class CampaignEvent(Base):
    __tablename__ = "campaign_events"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        UniqueConstraint("workspace_id", "offer_id", "code"),
        CheckConstraint("payout >= 0 AND revenue >= 0", name="ck_event_money"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(String(32))
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    model: Mapped[str] = mapped_column(String(3))
    payout: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    revenue: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    sale_percentage: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    repeatable: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CommissionRule(Base):
    __tablename__ = "commission_rules"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        ForeignKeyConstraint(["workspace_id", "publisher_id"], ["users.workspace_id", "users.id"]),
        Index("ix_commission_rule_priority", "workspace_id", "offer_id", "priority", "id"),
        CheckConstraint("payout >= 0 AND revenue >= 0", name="ck_rule_money"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(String(32))
    publisher_id: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    event_key: Mapped[str | None] = mapped_column(String(64))
    conditions: Mapped[dict] = mapped_column(JSON, default=dict)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    model: Mapped[str] = mapped_column(String(3))
    payout: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    revenue: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    sale_percentage: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    visible_to_publishers: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CampaignAsset(Base):
    __tablename__ = "campaign_assets"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        Index("ix_asset_campaign", "workspace_id", "offer_id", "id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(12))
    url: Mapped[str] = mapped_column(String(2048))
    description: Mapped[str] = mapped_column(String(1000), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CampaignDestination(Base):
    __tablename__ = "campaign_destinations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        Index("ix_destination_campaign", "workspace_id", "offer_id", "id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(160))
    url: Mapped[str] = mapped_column(String(2048))
    weight: Mapped[int] = mapped_column(Integer, default=1)
    countries: Mapped[list] = mapped_column(JSON, default=list)
    publisher_ids: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PartnerApiKey(Base):
    __tablename__ = "partner_api_keys"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "user_id"], ["users.workspace_id", "users.id"]),
        Index("ix_api_key_owner", "workspace_id", "user_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    user_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(16))
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReportTemplate(Base):
    __tablename__ = "report_templates"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "user_id"], ["users.workspace_id", "users.id"]),
        UniqueConstraint("workspace_id", "user_id", "name"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    user_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(100))
    configuration: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "user_id"], ["users.workspace_id", "users.id"]),
        Index("ix_notification_inbox", "workspace_id", "user_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    user_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(String(1000), default="")
    resource_id: Mapped[str | None] = mapped_column(String(32))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IntegrationConfig(Base):
    __tablename__ = "integration_configurations"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "user_id"], ["users.workspace_id", "users.id"]),
        UniqueConstraint("workspace_id", "user_id", "provider"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    user_id: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)
    credentials: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PostbackReceipt(Base):
    __tablename__ = "postback_receipts"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        Index("ix_postback_receipt_campaign", "workspace_id", "offer_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(String(32))
    click_id: Mapped[str | None] = mapped_column(String(32))
    transaction_id: Mapped[str] = mapped_column(String(120))
    event_key: Mapped[str] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(30))
    http_status: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Impression(Base):
    __tablename__ = "impressions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "link_id"], ["tracking_links.workspace_id", "tracking_links.id"]
        ),
        Index("ix_impression_workspace_created", "workspace_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    link_id: Mapped[str] = mapped_column(String(32))
    country: Mapped[str | None] = mapped_column(String(2))
    device: Mapped[str] = mapped_column(String(20))
    operating_system: Mapped[str] = mapped_column(String(20), default="other")
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    sub_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TrafficRule(Base):
    __tablename__ = "traffic_rules"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        Index("ix_traffic_rule_order", "workspace_id", "offer_id", "priority", "id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    priority: Mapped[int] = mapped_column(Integer, default=100)
    conditions: Mapped[dict] = mapped_column(JSON)
    action: Mapped[str] = mapped_column(String(10))
    phase: Mapped[str] = mapped_column(String(10))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PeriodCap(Base):
    __tablename__ = "period_caps"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        ForeignKeyConstraint(["workspace_id", "publisher_id"], ["users.workspace_id", "users.id"]),
        UniqueConstraint("workspace_id", "id"),
        CheckConstraint("amount > 0", name="ck_period_cap_amount"),
        Index("ix_period_cap_campaign", "workspace_id", "offer_id", "id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(String(32))
    publisher_id: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    event_key: Mapped[str | None] = mapped_column(String(64))
    metric: Mapped[str] = mapped_column(String(20))
    period: Mapped[str] = mapped_column(String(10))
    timezone: Mapped[str] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PeriodCapCounter(Base):
    __tablename__ = "period_cap_counters"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "cap_id"], ["period_caps.workspace_id", "period_caps.id"]
        ),
        UniqueConstraint("workspace_id", "cap_id", "bucket"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    cap_id: Mapped[str] = mapped_column(String(32))
    bucket: Mapped[str] = mapped_column(String(50))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)


class CampaignTarget(Base):
    """A sales goal for tracking progress; unlike caps it never blocks conversions."""

    __tablename__ = "campaign_targets"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        ForeignKeyConstraint(["workspace_id", "partner_id"], ["users.workspace_id", "users.id"]),
        UniqueConstraint("workspace_id", "id"),
        CheckConstraint("amount > 0", name="ck_campaign_target_amount"),
        CheckConstraint("ends_on >= starts_on", name="ck_campaign_target_dates"),
        Index("ix_campaign_target_campaign", "workspace_id", "offer_id", "id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(String(32))
    # Empty for the campaign's overall goal; otherwise the agency or partner it is split to.
    partner_id: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    event_key: Mapped[str] = mapped_column(String(64), default="initial")
    amount: Mapped[int] = mapped_column(Integer)
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Placement(Base):
    """A named post or spot (e.g. "Insta story 28 Oct") on an influencer's link, tracked as its sub ID."""

    __tablename__ = "placements"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "link_id"], ["tracking_links.workspace_id", "tracking_links.id"]
        ),
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "link_id", "sub_id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    link_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    platform: Mapped[str] = mapped_column(String(20))
    sub_id: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ManualResult(Base):
    """Sales a brand reported only as totals; kept apart from click-attributed conversions."""

    __tablename__ = "manual_results"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        UniqueConstraint("workspace_id", "id"),
        CheckConstraint("sales >= 0 AND earnings >= 0", name="ck_manual_result_values"),
        CheckConstraint("period_end >= period_start", name="ck_manual_result_dates"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(String(32))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    sales: Mapped[int] = mapped_column(Integer)
    earnings: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BlockedSource(Base):
    """A traffic source an account refuses to pay for, account-wide or for one campaign."""

    __tablename__ = "blocked_sources"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_blocked_source_workspace", "workspace_id", "active"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str | None] = mapped_column(String(32))
    preset: Mapped[str] = mapped_column(String(30))
    # Domain for custom_domain, source/utm_source value for custom_source; empty for presets.
    value: Mapped[str] = mapped_column(String(253), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SmartLink(Base):
    __tablename__ = "smart_links"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "publisher_id"], ["users.workspace_id", "users.id"]),
        Index("ix_smart_link_owner", "workspace_id", "publisher_id", "id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    publisher_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    children: Mapped[list] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AutomationRule(Base):
    __tablename__ = "automation_rules"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        ForeignKeyConstraint(["workspace_id", "user_id"], ["users.workspace_id", "users.id"]),
        Index("ix_automation_due", "workspace_id", "next_check_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    configuration: Mapped[dict] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AutomationLog(Base):
    __tablename__ = "automation_logs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "rule_id"], ["automation_rules.workspace_id", "automation_rules.id"]
        ),
        Index("ix_automation_log_created", "workspace_id", "rule_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    rule_id: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(20))
    observed: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
