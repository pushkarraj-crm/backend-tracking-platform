from sqlalchemy import select

from app.persistence.models import User
from app.persistence.network_models import (
    Notification,
)


def notify(db, workspace_id, user_ids, kind, title, message="", resource_id=None):
    ids = {i for i in user_ids if i}
    valid = list(
        db.scalars(
            select(User.id)
            .where(User.workspace_id == workspace_id, User.id.in_(ids), User.active.is_(True))
            .limit(100)
        )
    )
    db.add_all(
        Notification(
            workspace_id=workspace_id,
            user_id=user_id,
            kind=kind,
            title=title[:200],
            message=message[:1000],
            resource_id=resource_id,
        )
        for user_id in valid
    )


def notify_admins(db, workspace_id, kind, title, message="", resource_id=None):
    ids = list(
        db.scalars(
            select(User.id)
            .where(User.workspace_id == workspace_id, User.role == "admin", User.active.is_(True))
            .limit(50)
        )
    )
    notify(db, workspace_id, ids, kind, title, message, resource_id)
