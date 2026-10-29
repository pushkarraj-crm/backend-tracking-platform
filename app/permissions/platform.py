from fastapi import HTTPException


def platform_only(user):
    if user.role != "platform_admin":
        raise HTTPException(403, "Platform owner access required")
