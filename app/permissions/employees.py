from fastapi import HTTPException


def employee_capability(user, permission):
    if (
        user.role == "staff"
        and user.staff_permissions is not None
        and permission not in user.staff_permissions
    ):
        raise HTTPException(403, "Employee permission is required for this operation")
