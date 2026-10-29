from fastapi import HTTPException

from app.persistence.models import User


def staff_only(user: User):
    if user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Staff access required")
