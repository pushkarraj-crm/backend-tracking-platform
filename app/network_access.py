"""Narrow partner API scopes and employee restrictions, applied in addition to roles."""

from datetime import datetime, timezone, timedelta
import hashlib
from fastapi import HTTPException
from sqlalchemy import select
from .models import User
from .network_models import PartnerApiKey
from .services import utc

API_SCOPES = {
    "campaigns:read",
    "campaigns:apply",
    "reports:read",
    "conversions:read",
    "links:write",
    "invoices:read",
}
STAFF_PERMISSIONS = {
    "campaigns:read",
    "campaigns:write",
    "applications:review",
    "partners:read",
    "partners:write",
    "conversions:read",
    "conversions:review",
    "reports:read",
    "domains:read",
    "domains:write",
    "callbacks:read",
    "callbacks:write",
    "invoices:read",
    "fraud:read",
    "fraud:review",
    "deliveries:read",
    "deliveries:retry",
    "integrations:read",
    "integrations:write",
    "branding:read",
    "branding:write",
    "tools:read",
    "tools:write",
}


def scope_for(request):
    import re

    path = request.scope["path"].rstrip("/") or "/"
    method = request.method
    if method == "GET":
        if path.startswith(("/campaigns", "/offers", "/links", "/campaign-applications")):
            return "campaigns:read"
        if path.startswith("/reports/"):
            return "reports:read"
        if path == "/conversions":
            return "conversions:read"
        if path.startswith("/publisher-invoices"):
            return "invoices:read"
    if method == "POST":
        if path == "/links" or re.fullmatch(r"/campaigns/[a-f0-9]{32}/link", path):
            return "links:write"
        if re.fullmatch(r"/campaigns/[a-f0-9]{32}/apply", path):
            return "campaigns:apply"
    return None


def api_user(db, secret, request):
    error = HTTPException(401, "Invalid or expired API key", headers={"WWW-Authenticate": "Bearer"})
    if len(secret) > 128:
        raise error
    row = db.scalar(
        select(PartnerApiKey).where(
            PartnerApiKey.secret_hash == hashlib.sha256(secret.encode()).hexdigest()
        )
    )
    if not row or not row.active or utc(row.expires_at) <= datetime.now(timezone.utc):
        raise error
    user = db.get(User, row.user_id)
    if not user or not user.active or user.workspace_id != row.workspace_id or not user.api_enabled:
        raise error
    scope = scope_for(request)
    if scope is None or scope not in row.scopes:
        raise HTTPException(403, "API key does not permit this operation")
    request.state.api_key_id = row.id
    if row.last_used_at is None or utc(row.last_used_at) < datetime.now(timezone.utc) - timedelta(
        minutes=10
    ):
        row.last_used_at = datetime.now(timezone.utc)
        db.commit()
    return user


def employee_permission(user, request):
    if user.role != "staff" or user.staff_permissions is None:
        return
    path = request.scope["path"].rstrip("/") or "/"
    read = request.method in {"GET", "HEAD"}
    if path in {
        "/me",
        "/workspace",
        "/notifications",
        "/notifications/read-all",
    } or path.startswith(("/auth/", "/notifications/")):
        return
    if path.startswith("/campaign-applications"):
        permission = "campaigns:read" if read else "applications:review"
    elif path.startswith(("/campaigns", "/offers", "/links")):
        permission = "campaigns:read" if read else "campaigns:write"
    elif path.startswith("/users"):
        permission = "partners:read" if read else "partners:write"
    elif path.startswith(("/reports/", "/report-templates")):
        permission = "reports:read"
    elif path.startswith("/conversions"):
        permission = "conversions:read" if read else "conversions:review"
    elif path.startswith("/tracking-domains") or path.startswith("/landing-pages"):
        permission = "domains:read" if read else "domains:write"
    elif path.startswith(("/callbacks", "/webhooks", "/publisher-postback")):
        permission = "callbacks:read" if read else "callbacks:write"
    elif path.startswith("/publisher-invoices"):
        permission = "invoices:read" if read else None
    elif path.startswith("/fraud/"):
        permission = "fraud:read" if read else "fraud:review"
    elif path.startswith(("/deliveries", "/callback-deliveries")):
        permission = "deliveries:read" if read else "deliveries:retry"
    elif path.startswith("/postback-receipts"):
        permission = "deliveries:read"
    elif path.startswith("/integrations"):
        permission = "integrations:read" if read else "integrations:write"
    elif path.startswith("/branding"):
        permission = "branding:read" if read else "branding:write"
    elif path.startswith(("/tools/", "/smart-links")):
        permission = "tools:read" if read else "tools:write"
    else:
        permission = None
    if permission is None or permission not in user.staff_permissions:
        raise HTTPException(403, "Employee permission is required for this operation")
