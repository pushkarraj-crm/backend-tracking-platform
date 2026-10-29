from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.security import DB, Actor, passwords
from app.core.services import audit, quota
from app.features.auth.router import issue_action
from app.features.partners.presentation import output_user
from app.permissions.resources import entitled, owned
from app.persistence.models import AuthSession, User, Workspace
from app.shared.schemas import ActiveUpdate, UserCreate

settings = get_settings()


router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/me")
def me(user: Actor):
    return output_user(user)


@router.post("/users", status_code=201)
def create_user(body: UserCreate, db: DB, user: Actor):
    if user.role not in {"admin", "affiliate"}:
        raise HTTPException(403, "Admin or affiliate access required")
    if body.role == "advertiser" and db.get(Workspace, user.workspace_id).account_type == "brand":
        raise HTTPException(403, "A brand account is its own advertiser")
    if (
        body.role in {"advertiser", "affiliate", "publisher"}
        and db.get(Workspace, user.workspace_id).account_type == "influencer"
    ):
        raise HTTPException(403, "Influencer accounts add brands and links, not partner logins")
    parent_id = body.parent_user_id
    if user.role == "affiliate":
        if body.role != "publisher" or (parent_id and parent_id != user.id):
            raise HTTPException(403, "Affiliates can create only their own publishers")
        parent_id = user.id
    if body.role == "publisher":
        if parent_id and owned(db, User, parent_id, user).role != "affiliate":
            raise HTTPException(400, "Publisher requires an affiliate parent")
    elif parent_id:
        raise HTTPException(400, "Only publishers can have a parent")
    quota(db, user, User, "users")
    from app.persistence.models import TeamMember

    if db.scalar(select(TeamMember.id).where(TeamMember.email == str(body.email).lower())):
        raise HTTPException(409, "Email is already registered")
    member = User(
        workspace_id=user.workspace_id,
        email=str(body.email).lower(),
        password_hash=passwords.hash(body.password),
        role=body.role,
        parent_user_id=parent_id,
        email_verified=not settings.require_email_verification,
    )
    db.add(member)
    try:
        db.flush()
        audit(db, user, "user.created", member.id, member.role)
        if settings.require_email_verification:
            issue_action(db, member, "verify")
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Email is already registered")
    return output_user(member)


@router.get("/users")
def list_users(
    db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    entitled(db, user)
    if user.role not in {"admin", "staff", "affiliate"}:
        raise HTTPException(403, "User management access required")
    query = select(User).where(
        User.workspace_id == user.workspace_id,
        User.role != "platform_admin",
        # A brand's built-in advertiser record is not a person to manage.
        User.email.not_like("%.house.invalid"),
    )
    if user.role == "affiliate":
        query = query.where(User.parent_user_id == user.id)
    return [output_user(u) for u in db.scalars(query.order_by(User.id).offset(offset).limit(limit))]


@router.patch("/users/{user_id}")
def set_user_active(user_id: str, body: ActiveUpdate, db: DB, user: Actor):
    entitled(db, user)
    if user.role not in {"admin", "affiliate"}:
        raise HTTPException(403, "Admin or affiliate access required")
    if user_id == user.id:
        raise HTTPException(400, "Cannot deactivate your own account")
    member = owned(db, User, user_id, user)
    if user.role == "affiliate" and (
        member.role != "publisher" or member.parent_user_id != user.id
    ):
        raise HTTPException(404, "Publisher not found")
    member.active = body.active
    member.auth_version += 1
    db.execute(update(AuthSession).where(AuthSession.user_id == member.id).values(revoked=True))
    audit(db, user, "user.active", member.id, str(body.active))
    db.commit()
    return {**output_user(member), "active": member.active}
