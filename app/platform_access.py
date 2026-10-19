"""Platform-owner access management across explicitly selected network databases."""

from contextlib import contextmanager
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, model_validator
from sqlalchemy import select, func, update
from .security import DB, Actor
from .schemas import StrictModel
from .models import (
    Workspace,
    User,
    AuthSession,
    AuditEvent,
    Offer,
    Conversion,
    PublisherInvoice,
    TrackingDomain,
)
from .network_models import PartnerApiKey
from .network_access import STAFF_PERMISSIONS
from .portals import platform_only, workspace_output
from .routes import output_user
from .config import get_settings

router = APIRouter(prefix="/platform/networks", tags=["Super admin"])


@contextmanager
def network_db(control, workspace_id):
    """Select a database only from the server-owned directory, never a supplied URL."""
    from .tenancy import Company, company_sessions

    if get_settings().company_databases:
        company = control.scalar(
            select(Company).where(Company.workspace_id == workspace_id, Company.status == "active")
        )
        if not company:
            raise HTTPException(404, "Network not found")
        with company_sessions(company).begin() as session:
            if not session.get(Workspace, workspace_id):
                raise HTTPException(404, "Network not found")
            yield session
    else:
        if not control.get(Workspace, workspace_id):
            raise HTTPException(404, "Network not found")
        yield control
        control.commit()


class AccessUpdate(StrictModel):
    role: Literal["admin", "staff", "advertiser", "affiliate", "publisher"]
    active: bool
    api_enabled: bool = False
    staff_permissions: list[str] | None = Field(default=None, max_length=30)
    parent_user_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    reason: str = Field(min_length=8, max_length=160)

    @model_validator(mode="after")
    def valid(self):
        if self.staff_permissions is not None and (
            set(self.staff_permissions) - STAFF_PERMISSIONS
            or len(set(self.staff_permissions)) != len(self.staff_permissions)
        ):
            raise ValueError("Select unique supported staff permissions")
        if self.role != "staff" and self.staff_permissions is not None:
            raise ValueError("Module restrictions apply to staff accounts only")
        if self.role != "publisher" and self.parent_user_id:
            raise ValueError("Only a publisher can have a parent affiliate")
        return self


@router.get("/permissions")
def permissions(db: DB, user: Actor):
    platform_only(user)
    return {
        "roles": ["admin", "staff", "advertiser", "affiliate", "publisher"],
        "staff_permissions": sorted(STAFF_PERMISSIONS),
    }


@router.get("/{workspace_id}/users")
def users(
    workspace_id: str,
    db: DB,
    user: Actor,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    platform_only(user)
    with network_db(db, workspace_id) as target:
        return [
            output_user(row)
            for row in target.scalars(
                select(User)
                .where(User.workspace_id == workspace_id)
                .order_by(User.email, User.id)
                .offset(offset)
                .limit(limit)
            )
        ]


@router.patch("/{workspace_id}/users/{user_id}/access")
def change_access(workspace_id: str, user_id: str, body: AccessUpdate, db: DB, user: Actor):
    platform_only(user)
    with network_db(db, workspace_id) as target:
        # Serialize last-admin and hierarchy checks within this network.
        target.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update())
        member = target.scalar(
            select(User)
            .where(User.id == user_id, User.workspace_id == workspace_id)
            .with_for_update()
        )
        if not member:
            raise HTTPException(404, "Account not found in this network")
        if member.role == "platform_admin" or member.id == user.id:
            raise HTTPException(
                409, "Super admin accounts cannot be changed through network access controls"
            )
        if member.role == "admin" and member.active and (body.role != "admin" or not body.active):
            others = target.scalar(
                select(func.count())
                .select_from(User)
                .where(
                    User.workspace_id == workspace_id,
                    User.role == "admin",
                    User.active.is_(True),
                    User.id != member.id,
                )
            )
            if not others:
                raise HTTPException(409, "Assign another active network admin first")
        if (body.role != "affiliate" or not body.active) and target.scalar(
            select(User.id)
            .where(User.workspace_id == workspace_id, User.parent_user_id == member.id)
            .limit(1)
        ):
            raise HTTPException(409, "Reassign this affiliate's publishers first")
        if (
            member.role == "advertiser"
            and body.role not in {"admin", "advertiser"}
            and target.scalar(
                select(Offer.id)
                .where(
                    Offer.workspace_id == workspace_id,
                    Offer.advertiser_id == member.id,
                    Offer.active.is_(True),
                )
                .limit(1)
            )
        ):
            raise HTTPException(409, "Pause or reassign the advertiser's active campaigns first")
        if body.parent_user_id:
            parent = target.get(User, body.parent_user_id)
            if (
                not parent
                or parent.id == member.id
                or parent.workspace_id != workspace_id
                or parent.role != "affiliate"
                or not parent.active
            ):
                raise HTTPException(422, "Select an active affiliate in the same network")
        before = member.role
        member.role = body.role
        member.active = body.active
        member.api_enabled = body.api_enabled
        member.staff_permissions = body.staff_permissions
        member.parent_user_id = body.parent_user_id
        member.auth_version += 1
        target.execute(
            update(AuthSession)
            .where(AuthSession.user_id == member.id, AuthSession.workspace_id == workspace_id)
            .values(revoked=True)
        )
        target.execute(
            update(PartnerApiKey)
            .where(PartnerApiKey.user_id == member.id, PartnerApiKey.workspace_id == workspace_id)
            .values(active=False)
        )
        target.add(
            AuditEvent(
                workspace_id=workspace_id,
                actor_id=user.id,
                action="platform.user_access",
                resource_id=member.id,
                detail=f"{before}->{body.role}; active={body.active}; {body.reason}"[:200],
            )
        )
        target.flush()
        return {**output_user(member), "sessions_revoked": True, "api_keys_revoked": True}


@router.get("/{workspace_id}/{dataset}")
def network_records(
    workspace_id: str,
    dataset: Literal["overview", "campaigns", "conversions", "invoices", "domains", "audit"],
    db: DB,
    user: Actor,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    platform_only(user)
    with network_db(db, workspace_id) as target:
        if dataset == "overview":
            return workspace_output(target.get(Workspace, workspace_id))
        model, fields = {
            "campaigns": (
                Offer,
                [
                    "id",
                    "name",
                    "advertiser_id",
                    "campaign_model",
                    "active",
                    "currency",
                    "payout",
                    "revenue",
                ],
            ),
            "conversions": (
                Conversion,
                [
                    "id",
                    "offer_id",
                    "event_key",
                    "transaction_id",
                    "status",
                    "currency",
                    "payout",
                    "revenue",
                ],
            ),
            "invoices": (
                PublisherInvoice,
                ["id", "number", "advertiser_id", "publisher_id", "status", "amount", "currency"],
            ),
            "domains": (
                TrackingDomain,
                ["id", "hostname", "advertiser_id", "purpose", "active", "verified_at"],
            ),
            "audit": (
                AuditEvent,
                ["id", "actor_id", "action", "resource_id", "detail", "created_at"],
            ),
        }[dataset]
        from decimal import Decimal

        rows = target.scalars(
            select(model)
            .where(model.workspace_id == workspace_id)
            .order_by(model.id)
            .offset(offset)
            .limit(limit)
        )
        return [
            {
                field: str(getattr(row, field))
                if isinstance(getattr(row, field), Decimal)
                else getattr(row, field)
                for field in fields
            }
            for row in rows
        ]
