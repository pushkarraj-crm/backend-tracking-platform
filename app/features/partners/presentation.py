def output_user(user):
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "active": user.active,
        "parent_user_id": user.parent_user_id,
        "email_verified": user.email_verified,
        "name": user.full_name,
        "api_enabled": user.api_enabled,
        "staff_permissions": user.staff_permissions,
    }
