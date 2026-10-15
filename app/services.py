"""Shared authorization, tenant entitlements and transactional delivery helpers."""

import json
from datetime import datetime, timezone
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select, func
from .config import get_settings
from .models import Workspace, AuditEvent, OutboxEvent


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def cipher():
    key = get_settings().encryption_key
    if not key:
        raise HTTPException(503, "Encryption configuration is required")
    return Fernet(key.encode())


def encrypt(value):
    return cipher().encrypt(value.encode()).decode()


def decrypt(value):
    return cipher().decrypt(value.encode()).decode()


def entitlement(workspace):
    now = datetime.now(timezone.utc)
    if not workspace or not workspace.active:
        raise HTTPException(403, "Workspace is suspended")
    if (
        workspace.subscription_status == "pending"
        and workspace.paid_until
        and workspace.billing_grace_until
        and utc(workspace.billing_grace_until) > now
    ):
        return
    if workspace.plan == "trial" and workspace.subscription_status in {
        "trial",
        "created",
        "authenticated",
        "pending",
    }:
        if not workspace.trial_ends_at or utc(workspace.trial_ends_at) <= now:
            raise HTTPException(402, "Your 15-day trial has ended. Subscribe to continue.")
    elif (
        workspace.subscription_status != "active"
        or not workspace.paid_until
        or utc(workspace.paid_until) <= now
    ):
        raise HTTPException(402, "An active subscription is required")


def quota(db, user, model, resource):
    # Serialize resource creation per workspace to prevent concurrent quota bypass.
    workspace = db.scalar(
        select(Workspace).where(Workspace.id == user.workspace_id).with_for_update()
    )
    entitlement(workspace)
    maximum = get_settings().plan_limits.get(workspace.plan, {}).get(resource)
    if maximum is None:
        raise HTTPException(403, "Plan configuration is missing")
    count = db.scalar(
        select(func.count()).select_from(model).where(model.workspace_id == workspace.id)
    )
    if count >= maximum:
        raise HTTPException(409, f"Plan {resource} limit reached")


def audit(db, user, action, resource_id, detail=""):
    db.add(
        AuditEvent(
            workspace_id=user.workspace_id,
            actor_id=user.id,
            action=action,
            resource_id=resource_id,
            detail=detail[:200],
        )
    )


def enqueue(db, workspace_id, kind, payload):
    event = OutboxEvent(workspace_id=workspace_id, kind=kind, payload=encrypt(json.dumps(payload)))
    db.add(event)
    return event
