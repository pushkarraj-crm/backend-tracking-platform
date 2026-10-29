"""Login as partner, agency team logins and account managers."""

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field
from sqlalchemy import select, update

from app.core.security import DB, Actor, key_hash, passwords, token_for
from app.core.services import audit
from app.features.accounts.router import session_only
from app.permissions.resources import entitled, owned
from app.persistence.models import AuthSession, TeamMember, User, identifier
from app.shared.schemas import ActiveUpdate, Credentials, StrictModel

router = APIRouter(tags=["Teams and account managers"])
PARTNER_ROLES = {"advertiser", "affiliate", "publisher"}
TEAM_OWNER_ROLES = PARTNER_ROLES  # agencies are publishers in brand accounts
LOGIN_AS_MINUTES = 60


def partner(db, user, user_id):
    target = owned(db, User, user_id, user)
    if target.role not in PARTNER_ROLES or target.email.endswith(".house.invalid"):
        raise HTTPException(404, "Partner not found")
    return target


@router.post("/users/{user_id}/login-as")
def login_as(user_id: str, request: Request, db: DB, user: Actor):
    """An admin opens a partner's dashboard as that partner, for one hour."""
    session_only(request)  # no API keys, and no "log in as" from inside another one
    entitled(db, user)
    if user.role != "admin":
        raise HTTPException(403, "Network admin required")
    target = partner(db, user, user_id)
    if not target.active:
        raise HTTPException(409, "This partner's account is disabled")
    session = AuthSession(
        id=identifier(),
        workspace_id=target.workspace_id,
        user_id=target.id,
        # No refresh cookie is issued; this random hash only fills the required column.
        refresh_hash=key_hash(secrets.token_urlsafe(48)),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=LOGIN_AS_MINUTES),
        acted_by=user.id,
    )
    db.add(session)
    audit(db, user, "user.login_as", target.id, target.email)
    db.commit()
    return {
        "access_token": token_for(target, session.id, LOGIN_AS_MINUTES),
        "token_type": "bearer",
        "expires_in": LOGIN_AS_MINUTES * 60,
        "user": {
            "id": target.id,
            "email": target.email,
            "role": target.role,
            "parent_user_id": target.parent_user_id,
            "team_member": None,
        },
    }


class TeamMemberBody(Credentials):
    name: str = Field(default="", max_length=120)


class PasswordBody(StrictModel):
    password: str = Field(min_length=12, max_length=128)


def team_output(member):
    return {
        "id": member.id,
        "email": member.email,
        "name": member.full_name or "",
        "active": member.active,
        "created_at": member.created_at,
        "last_login_at": member.last_login_at,
    }


def team_owner(request, user):
    session_only(request)  # team members cannot manage the team themselves
    if user.role not in TEAM_OWNER_ROLES:
        raise HTTPException(403, "Teams are for partner accounts")


def own_member(db, user, member_id):
    member = db.get(TeamMember, member_id)
    if not member or member.owner_id != user.id or member.workspace_id != user.workspace_id:
        raise HTTPException(404, "Team member not found")
    return member


def end_sessions(db, member):
    db.execute(
        update(AuthSession).where(AuthSession.team_member_id == member.id).values(revoked=True)
    )


@router.get("/team")
def team(request: Request, db: DB, user: Actor):
    team_owner(request, user)
    rows = db.scalars(
        select(TeamMember).where(TeamMember.owner_id == user.id).order_by(TeamMember.created_at)
    )
    return [team_output(m) for m in rows]


@router.post("/team", status_code=201)
def add_team_member(body: TeamMemberBody, request: Request, db: DB, user: Actor):
    team_owner(request, user)
    entitled(db, user)
    email = str(body.email).lower()
    # One login per email address across people and team members.
    if db.scalar(select(User.id).where(User.email == email)) or db.scalar(
        select(TeamMember.id).where(TeamMember.email == email)
    ):
        raise HTTPException(409, "Email is already registered")
    if db.scalar(select(TeamMember.id).where(TeamMember.owner_id == user.id).offset(24).limit(1)):
        raise HTTPException(409, "A team can have up to 25 members")
    member = TeamMember(
        workspace_id=user.workspace_id,
        owner_id=user.id,
        email=email,
        full_name=body.name.strip() or None,
        password_hash=passwords.hash(body.password),
    )
    db.add(member)
    db.flush()
    audit(db, user, "team.member_added", member.id, email)
    db.commit()
    return team_output(member)


@router.patch("/team/{member_id}")
def set_team_member_active(
    member_id: str, body: ActiveUpdate, request: Request, db: DB, user: Actor
):
    team_owner(request, user)
    member = own_member(db, user, member_id)
    member.active = body.active
    if not body.active:
        end_sessions(db, member)
    audit(db, user, "team.member_active", member.id, str(body.active))
    db.commit()
    return team_output(member)


@router.put("/team/{member_id}/password")
def reset_team_member_password(
    member_id: str, body: PasswordBody, request: Request, db: DB, user: Actor
):
    team_owner(request, user)
    member = own_member(db, user, member_id)
    member.password_hash = passwords.hash(body.password)
    end_sessions(db, member)
    audit(db, user, "team.member_password", member.id)
    db.commit()
    return team_output(member)


class ManagerBody(StrictModel):
    manager_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


@router.put("/users/{user_id}/manager")
def assign_manager(user_id: str, body: ManagerBody, request: Request, db: DB, user: Actor):
    session_only(request)
    entitled(db, user)
    if user.role != "admin":
        raise HTTPException(403, "Network admin required")
    target = partner(db, user, user_id)
    if body.manager_id:
        manager = owned(db, User, body.manager_id, user)
        if manager.role not in {"admin", "staff"} or not manager.active:
            raise HTTPException(422, "Choose an active admin or staff member")
    target.manager_id = body.manager_id
    audit(db, user, "user.manager", target.id, body.manager_id or "none")
    db.commit()
    return {"id": target.id, "manager_id": target.manager_id}


@router.get("/account-manager")
def account_manager(db: DB, user: Actor):
    """The partner's own account manager, shown on their dashboard."""
    manager = db.get(User, user.manager_id) if user.manager_id else None
    if not manager or not manager.active or manager.workspace_id != user.workspace_id:
        return None
    return {"name": manager.full_name or "", "email": manager.email}


@router.get("/managed-partners")
def managed_partners(
    db: DB, user: Actor, manager_id: str | None = Query(None, pattern=r"^[a-f0-9]{32}$")
):
    """Partners assigned to a manager (default: the signed-in admin or staff member)."""
    entitled(db, user)
    if user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Network staff required")
    from app.features.partners.presentation import output_user

    rows = db.scalars(
        select(User)
        .where(User.workspace_id == user.workspace_id, User.manager_id == (manager_id or user.id))
        .order_by(User.email)
        .limit(200)
    )
    return [output_user(u) for u in rows]
