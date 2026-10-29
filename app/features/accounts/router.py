"""Network branding, scoped service keys, saved reports, inbox and integration drafts."""

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field, HttpUrl, SecretStr, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.security import DB, Actor, key_hash
from app.core.services import audit, decrypt, encrypt, entitlement
from app.features.campaigns.policy import safe_url
from app.features.reporting.router import Filters
from app.permissions.access import API_SCOPES, STAFF_PERMISSIONS
from app.permissions.resources import owned
from app.persistence.models import User, Workspace
from app.persistence.network_models import (
    IntegrationConfig,
    Notification,
    PartnerApiKey,
    ReportTemplate,
)
from app.shared.schemas import StrictModel

router = APIRouter(tags=["Network accounts and integrations"])


def session_only(request):
    if getattr(request.state, "api_key_id", None):
        raise HTTPException(403, "Use an interactive login session")
    if getattr(request.state, "acted_by", None) or getattr(request.state, "team_member_id", None):
        raise HTTPException(403, "Only the account owner can do this")


class ApiKeyBody(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    user_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    scopes: list[str] = Field(min_length=1, max_length=6)
    expires_in_days: int = Field(default=90, ge=1, le=365)

    @model_validator(mode="after")
    def valid(self):
        if set(self.scopes) - API_SCOPES or len(self.scopes) != len(set(self.scopes)):
            raise ValueError("Select unique supported API scopes")
        return self


@router.get("/api-keys")
def api_keys(request: Request, db: DB, user: Actor):
    session_only(request)
    query = select(PartnerApiKey).where(PartnerApiKey.workspace_id == user.workspace_id)
    if user.role != "admin":
        query = query.where(PartnerApiKey.user_id == user.id)
    return [
        {
            k: getattr(r, k)
            for k in [
                "id",
                "user_id",
                "name",
                "prefix",
                "scopes",
                "active",
                "expires_at",
                "last_used_at",
                "created_at",
            ]
        }
        for r in db.scalars(query.order_by(PartnerApiKey.created_at.desc()).limit(100))
    ]


@router.post("/api-keys", status_code=201)
def create_api_key(body: ApiKeyBody, request: Request, db: DB, user: Actor):
    session_only(request)
    entitlement(db.get(Workspace, user.workspace_id))
    owner = owned(db, User, body.user_id or user.id, user)
    if owner.id != user.id and user.role != "admin":
        raise HTTPException(403, "Only the network admin may issue keys to another account")
    if not owner.active or not owner.api_enabled or owner.role == "platform_admin":
        raise HTTPException(403, "Network admin must enable API access for this account first")
    db.scalar(select(User).where(User.id == owner.id).with_for_update())
    if (
        db.scalar(
            select(func.count())
            .select_from(PartnerApiKey)
            .where(PartnerApiKey.user_id == owner.id, PartnerApiKey.active.is_(True))
        )
        >= 10
    ):
        raise HTTPException(409, "Revoke an existing key before creating another")
    raw = "rk_" + secrets.token_urlsafe(40)
    row = PartnerApiKey(
        workspace_id=user.workspace_id,
        user_id=owner.id,
        name=body.name,
        prefix=raw[:12],
        secret_hash=key_hash(raw),
        scopes=body.scopes,
        expires_at=datetime.now(timezone.utc) + timedelta(days=body.expires_in_days),
    )
    db.add(row)
    db.flush()
    audit(db, user, "api_key.created", row.id)
    db.commit()
    return {
        "id": row.id,
        "key": raw,
        "prefix": row.prefix,
        "expires_at": row.expires_at,
        "scopes": row.scopes,
    }


@router.delete("/api-keys/{key_id}")
def revoke_key(key_id: str, request: Request, db: DB, user: Actor):
    session_only(request)
    row = owned(db, PartnerApiKey, key_id, user)
    if row.user_id != user.id and user.role != "admin":
        raise HTTPException(404, "API key not found")
    row.active = False
    audit(db, user, "api_key.revoked", row.id)
    db.commit()
    return {"revoked": True}


class ApiAccessBody(StrictModel):
    enabled: bool


@router.put("/users/{user_id}/api-access")
def api_access(user_id: str, body: ApiAccessBody, request: Request, db: DB, user: Actor):
    session_only(request)
    if user.role != "admin":
        raise HTTPException(403, "Network admin required")
    target = owned(db, User, user_id, user)
    if target.role == "platform_admin":
        raise HTTPException(403, "Network API keys cannot represent platform owners")
    target.api_enabled = body.enabled
    if not body.enabled:
        db.execute(
            update(PartnerApiKey)
            .where(
                PartnerApiKey.workspace_id == user.workspace_id, PartnerApiKey.user_id == target.id
            )
            .values(active=False)
        )
    audit(db, user, "user.api_access", target.id, str(body.enabled))
    db.commit()
    return {"id": target.id, "api_enabled": target.api_enabled}


class PermissionsBody(StrictModel):
    permissions: list[str] | None = Field(default=None, max_length=30)

    @model_validator(mode="after")
    def valid(self):
        if self.permissions is not None and (
            set(self.permissions) - STAFF_PERMISSIONS
            or len(self.permissions) != len(set(self.permissions))
        ):
            raise ValueError("Select unique supported employee permissions")
        return self


@router.get("/employee-permissions")
def available_permissions(db: DB, user: Actor):
    if user.role != "admin":
        raise HTTPException(403, "Network admin required")
    return {"permissions": sorted(STAFF_PERMISSIONS), "api_scopes": sorted(API_SCOPES)}


@router.put("/users/{user_id}/permissions")
def permissions(user_id: str, body: PermissionsBody, request: Request, db: DB, user: Actor):
    session_only(request)
    if user.role != "admin":
        raise HTTPException(403, "Network admin required")
    target = owned(db, User, user_id, user)
    if target.role != "staff":
        raise HTTPException(
            422, "Permissions restrict staff accounts only; they cannot grant administrator roles"
        )
    target.staff_permissions = body.permissions
    audit(db, user, "user.permissions", target.id)
    db.commit()
    return {"id": target.id, "permissions": target.staff_permissions}


class BrandingBody(StrictModel):
    network_name: str = Field(min_length=1, max_length=120)
    accent_color: str = Field(default="#214f3f", pattern=r"^#[0-9a-fA-F]{6}$")
    logo_url: HttpUrl | None = None
    support_email: str = Field(default="", max_length=254)
    timezone: str = Field(default="UTC", max_length=64)
    terms_url: HttpUrl | None = None
    privacy_url: HttpUrl | None = None

    @model_validator(mode="after")
    def valid(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use a supported IANA timezone") from None
        if self.support_email:
            from email_validator import EmailNotValidError, validate_email

            try:
                self.support_email = validate_email(
                    self.support_email, check_deliverability=False
                ).normalized
            except EmailNotValidError:
                raise ValueError("Use a valid support email") from None
        for url in [self.logo_url, self.terms_url, self.privacy_url]:
            if url:
                safe_url(url)
        return self


@router.get("/branding")
def branding(db: DB, user: Actor):
    workspace = db.get(Workspace, user.workspace_id)
    return {"network_name": workspace.name, **(workspace.branding or {})}


@router.put("/branding")
def update_branding(body: BrandingBody, db: DB, user: Actor):
    if user.role != "admin":
        raise HTTPException(403, "Network admin required")
    workspace = db.scalar(
        select(Workspace).where(Workspace.id == user.workspace_id).with_for_update()
    )
    workspace.name = body.network_name
    workspace.branding = body.model_dump(mode="json", exclude={"network_name"})
    audit(db, user, "network.branding_updated", workspace.id)
    db.commit()
    return {"network_name": workspace.name, **workspace.branding}


class TemplateBody(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    dataset: Literal["clicks", "conversions", "impressions", "grouped"] = "grouped"
    lookback_days: int = Field(default=7, ge=1, le=366)
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
    ] = "offer"
    filters: Filters = Field(default_factory=Filters)

    @model_validator(mode="after")
    def valid(self):
        if self.filters.start or self.filters.end:
            raise ValueError("Saved templates use a relative lookback period; omit absolute dates")
        return self


@router.post("/report-templates", status_code=201)
def create_template(body: TemplateBody, db: DB, user: Actor):
    if user.role not in {"admin", "staff", "advertiser", "publisher", "affiliate"}:
        raise HTTPException(403, "Reporting role required")
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    if (
        db.scalar(
            select(func.count())
            .select_from(ReportTemplate)
            .where(ReportTemplate.user_id == user.id)
        )
        >= 50
    ):
        raise HTTPException(409, "Saved report limit reached")
    row = ReportTemplate(
        workspace_id=user.workspace_id,
        user_id=user.id,
        name=body.name,
        configuration=body.model_dump(mode="json", exclude={"name"}),
    )
    db.add(row)
    try:
        db.flush()
        audit(db, user, "report_template.created", row.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A report template already uses that name") from None
    return {"id": row.id, "name": row.name, **row.configuration}


@router.get("/report-templates")
def templates(db: DB, user: Actor):
    return [
        {"id": r.id, "name": r.name, **r.configuration}
        for r in db.scalars(
            select(ReportTemplate)
            .where(
                ReportTemplate.workspace_id == user.workspace_id, ReportTemplate.user_id == user.id
            )
            .order_by(ReportTemplate.name)
            .limit(50)
        )
    ]


def own_template(db, user, template_id):
    row = owned(db, ReportTemplate, template_id, user)
    if row.user_id != user.id:
        raise HTTPException(404, "Report template not found")
    return row


@router.delete("/report-templates/{template_id}")
def remove_template(template_id: str, db: DB, user: Actor):
    row = own_template(db, user, template_id)
    audit(db, user, "report_template.deleted", row.id)
    db.delete(row)
    db.commit()
    return {"deleted": True}


@router.get("/report-templates/{template_id}/run")
def run_template(
    template_id: str,
    db: DB,
    user: Actor,
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(50, ge=1, le=100),
):
    from app.features.reporting import router as reporting

    row = own_template(db, user, template_id)
    config = TemplateBody(name=row.name, **row.configuration)
    end = datetime.now(timezone.utc)
    filters = config.filters.model_copy(
        update={"start": end - timedelta(days=config.lookback_days), "end": end}
    )
    if config.dataset == "grouped":
        return reporting.grouped(db, user, filters, config.group_by, limit)
    return getattr(reporting, config.dataset)(db, user, filters, offset, limit)


@router.get("/notifications")
def inbox(
    db: DB,
    user: Actor,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    unread: bool = False,
):
    query = select(Notification).where(
        Notification.workspace_id == user.workspace_id, Notification.user_id == user.id
    )
    if unread:
        query = query.where(Notification.read_at.is_(None))
    return [
        {
            k: getattr(n, k)
            for k in ["id", "kind", "title", "message", "resource_id", "read_at", "created_at"]
        }
        for n in db.scalars(
            query.order_by(Notification.created_at.desc(), Notification.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@router.post("/notifications/{notification_id}/read")
def read_notification(notification_id: str, db: DB, user: Actor):
    row = owned(db, Notification, notification_id, user)
    if row.user_id != user.id:
        raise HTTPException(404, "Notification not found")
    row.read_at = row.read_at or datetime.now(timezone.utc)
    db.commit()
    return {"id": row.id, "read": True}


@router.post("/notifications/read-all")
def read_all(db: DB, user: Actor):
    db.execute(
        update(Notification)
        .where(
            Notification.workspace_id == user.workspace_id,
            Notification.user_id == user.id,
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(timezone.utc))
    )
    db.commit()
    return {"read": True}


PROVIDERS = {
    "meta_ads": {
        "name": "Meta Conversions API",
        "fields": ["dataset_id", "test_event_code"],
        "secrets": ["access_token"],
        "guide": "https://developers.facebook.com/docs/marketing-api/conversions-api/",
    },
    "google_ads": {
        "name": "Google offline conversions (setup review required)",
        "fields": [
            "customer_id",
            "conversion_action",
            "client_id",
            "login_customer_id",
            "cloud_project_number",
            "ingestion_method",
        ],
        "secrets": ["developer_token", "client_secret", "refresh_token"],
        "guide": "https://developers.google.com/google-ads/api/docs/conversions/upload-clicks",
    },
    "appsflyer": {
        "name": "AppsFlyer",
        "fields": ["app_id", "media_source"],
        "secrets": ["api_token", "click_signing_key"],
        "guide": "https://knowledgebase.offer18.com/network/mobile-attribution-integration/appsflyer",
    },
    "adjust": {
        "name": "Adjust",
        "fields": ["app_token"],
        "secrets": ["api_token"],
        "guide": "https://knowledgebase.offer18.com/network/mobile-attribution-integration/adjust",
    },
    "branch": {
        "name": "Branch",
        "fields": ["app_id"],
        "secrets": ["api_key"],
        "guide": "https://knowledgebase.offer18.com/network/mobile-attribution-integration/branch",
    },
    "singular": {
        "name": "Singular",
        "fields": ["app_id"],
        "secrets": ["api_key"],
        "guide": "https://knowledgebase.offer18.com/network/mobile-attribution-integration/singular",
    },
    "kochava": {
        "name": "Kochava",
        "fields": ["app_id"],
        "secrets": ["api_key"],
        "guide": "https://knowledgebase.offer18.com/network/mobile-attribution-integration/kochava",
    },
    "tiktok": {
        "name": "TikTok Events API",
        "fields": ["pixel_id"],
        "secrets": ["access_token"],
        "guide": "https://knowledgebase.offer18.com/affiliate/integration/tiktok-event-api",
    },
    "snapchat": {
        "name": "Snapchat Conversions API",
        "fields": ["pixel_id"],
        "secrets": ["access_token"],
        "guide": "https://knowledgebase.offer18.com/affiliate/integration/snapchat-conversion-api",
    },
    "slack": {
        "name": "Slack notifications",
        "fields": ["channel_label"],
        "secrets": ["webhook_url"],
        "guide": "https://api.slack.com/messaging/webhooks",
    },
    "telegram": {
        "name": "Telegram notifications",
        "fields": ["chat_id"],
        "secrets": ["bot_token"],
        "guide": "https://core.telegram.org/bots",
    },
    "smtp": {
        "name": "Network notification email",
        "fields": ["host", "port", "username", "from_address"],
        "secrets": ["password"],
        "guide": "https://github.com/pushkarraj-crm/backend-tracking-platform/blob/main/docs/network-integrations.md",
    },
    "object_storage": {
        "name": "Creative object storage",
        "fields": ["endpoint", "bucket", "region", "public_base_url"],
        "secrets": ["access_key_id", "secret_access_key"],
        "guide": "https://github.com/pushkarraj-crm/backend-tracking-platform/blob/main/docs/network-integrations.md",
    },
}


class IntegrationBody(StrictModel):
    configuration: dict[str, str] = Field(default_factory=dict, max_length=20)
    secrets: dict[str, SecretStr] | None = Field(default=None, max_length=10)

    @model_validator(mode="after")
    def valid(self):
        if any(len(k) > 60 or len(v) > 2048 for k, v in self.configuration.items()):
            raise ValueError("Configuration values are too large")
        if self.secrets and any(
            len(k) > 60 or len(v.get_secret_value()) > 8192 for k, v in self.secrets.items()
        ):
            raise ValueError("Credential values are too large")
        return self


@router.get("/integrations/catalog")
def integration_catalog(db: DB, user: Actor):
    return [
        {
            "provider": key,
            **value,
            "activation": "disabled_until_provider_verification",
            "setup_note": "Choose the currently supported ingestion API for your account before adapter implementation. New Google offline-conversion integrations require Data Manager API review."
            if key == "google_ads"
            else "Save identifiers now; test a provider-specific adapter before live delivery.",
            "live_delivery_supported": False,
        }
        for key, value in PROVIDERS.items()
    ]


def integration_output(row):
    secret_keys = sorted(json.loads(decrypt(row.credentials))) if row.credentials else []
    requirements = PROVIDERS[row.provider]
    missing = sorted(set(requirements["secrets"]) - set(secret_keys))
    return {
        "id": row.id,
        "provider": row.provider,
        "configuration": row.configuration,
        "credential_fields_present": secret_keys,
        "missing_credentials": missing,
        "status": "credentials_required" if missing else "verification_required",
        "active": False,
        "updated_at": row.updated_at,
    }


@router.get("/integrations")
def integrations(db: DB, user: Actor):
    return [
        integration_output(row)
        for row in db.scalars(
            select(IntegrationConfig)
            .where(
                IntegrationConfig.workspace_id == user.workspace_id,
                IntegrationConfig.user_id == user.id,
            )
            .order_by(IntegrationConfig.provider)
        )
    ]


@router.put("/integrations/{provider}")
def save_integration(provider: str, body: IntegrationBody, request: Request, db: DB, user: Actor):
    session_only(request)
    if user.role not in {"admin", "advertiser", "publisher", "affiliate"}:
        raise HTTPException(403, "Only an account owner may store provider credentials")
    if provider not in PROVIDERS:
        raise HTTPException(404, "Unknown integration")
    spec = PROVIDERS[provider]
    if set(body.configuration) - set(spec["fields"]) or (
        body.secrets and set(body.secrets) - set(spec["secrets"])
    ):
        raise HTTPException(422, "Unknown provider configuration field")
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    row = db.scalar(
        select(IntegrationConfig).where(
            IntegrationConfig.workspace_id == user.workspace_id,
            IntegrationConfig.user_id == user.id,
            IntegrationConfig.provider == provider,
        )
    )
    if not row:
        row = IntegrationConfig(workspace_id=user.workspace_id, user_id=user.id, provider=provider)
        db.add(row)
    row.configuration = body.configuration
    row.updated_at = datetime.now(timezone.utc)
    if body.secrets is not None:
        values = {
            key: value.get_secret_value()
            for key, value in body.secrets.items()
            if value.get_secret_value()
        }
        row.credentials = encrypt(json.dumps(values)) if values else None
    db.flush()
    audit(db, user, "integration.draft_saved", row.id, provider)
    db.commit()
    return integration_output(row)


@router.delete("/integrations/{provider}")
def remove_integration(provider: str, request: Request, db: DB, user: Actor):
    session_only(request)
    row = db.scalar(
        select(IntegrationConfig).where(
            IntegrationConfig.workspace_id == user.workspace_id,
            IntegrationConfig.user_id == user.id,
            IntegrationConfig.provider == provider,
        )
    )
    if row:
        audit(db, user, "integration.removed", row.id, provider)
        db.delete(row)
        db.commit()
    return {"deleted": True}
