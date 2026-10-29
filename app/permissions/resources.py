from fastapi import HTTPException
from sqlalchemy import select

from app.core.services import entitlement
from app.persistence.models import User, Workspace


def entitled(db, user):
    entitlement(db.get(Workspace, user.workspace_id))


def visible_users(user):
    query = select(User.id).where(User.workspace_id == user.workspace_id)
    if user.role == "affiliate":
        query = query.where((User.id == user.id) | (User.parent_user_id == user.id))
    elif user.role == "publisher":
        query = query.where(User.id == user.id)
    else:
        raise HTTPException(403, "Affiliate or publisher required")
    return query


def owned(db, model, object_id, user):
    query = select(model).where(model.id == object_id, model.workspace_id == user.workspace_id)
    if model is User:
        # Platform identities are not tenant-managed partners, even when an old
        # owner activation retained the account's original workspace reference.
        query = query.where(User.role != "platform_admin")
    result = db.scalar(query)
    if not result:
        raise HTTPException(404, "Resource not found")
    return result
