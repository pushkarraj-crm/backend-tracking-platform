import hashlib
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Annotated
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pwdlib import PasswordHash
from sqlalchemy.orm import Session
from .config import get_settings
from .db import get_db
from .models import User, AuthSession, Workspace
from .services import utc


class BoundedPasswords:
    """Bound concurrent Argon2 memory use during distributed login attacks."""

    def __init__(self):
        self.hasher = PasswordHash.recommended()
        self.slots = threading.BoundedSemaphore(4)

    def run(self, method, *args):
        if not self.slots.acquire(timeout=5):
            raise HTTPException(503, "Authentication is busy; retry shortly")
        try:
            return method(*args)
        finally:
            self.slots.release()

    def hash(self, password):
        return self.run(self.hasher.hash, password)

    def verify(self, password, encoded):
        return self.run(self.hasher.verify, password, encoded)


passwords = BoundedPasswords()
dummy_hash = passwords.hash(secrets.token_urlsafe(32))
bearer = HTTPBearer(auto_error=False)
DB = Annotated[Session, Depends(get_db)]


def key_hash(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


def token_for(user: User, session_id: str):
    from .tenancy import company_context

    company = company_context.get()
    settings = get_settings()
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": user.id,
            "tenant": user.workspace_id,
            "company": company.slug if company else None,
            "sid": session_id,
            "ver": user.auth_version,
            "iat": now,
            "exp": now + timedelta(minutes=settings.token_minutes),
            "iss": "affiliate-platform",
            "aud": "affiliate-api",
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def current_user(
    db: DB, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
):
    error = HTTPException(
        401, "Invalid or expired credentials", headers={"WWW-Authenticate": "Bearer"}
    )
    if not credentials:
        raise error
    claims = None
    for secret in filter(None, (get_settings().jwt_secret, get_settings().jwt_previous_secret)):
        try:
            claims = jwt.decode(
                credentials.credentials,
                secret,
                algorithms=["HS256"],
                audience="affiliate-api",
                issuer="affiliate-platform",
                options={"require": ["sub", "tenant", "sid", "ver", "exp", "iat", "iss", "aud"]},
            )
            break
        except jwt.InvalidTokenError:
            continue
    if claims is None:
        raise error
    from .tenancy import company_context

    company = company_context.get()
    if claims.get("company") != (company.slug if company else None):
        raise error
    user = db.get(User, claims["sub"])
    session = db.get(AuthSession, claims["sid"])
    if (
        not user
        or not user.active
        or user.workspace_id != claims["tenant"]
        or user.auth_version != claims["ver"]
    ):
        raise error
    if (
        not session
        or session.revoked
        or session.user_id != user.id
        or utc(session.expires_at) <= datetime.now(timezone.utc)
    ):
        raise error
    workspace = db.get(Workspace, user.workspace_id)
    if not workspace or not workspace.active:
        raise HTTPException(403, "Workspace is suspended")
    if user.parent_user_id:
        parent = db.get(User, user.parent_user_id)
        if not parent or not parent.active:
            raise HTTPException(403, "Parent affiliate is disabled")
    if get_settings().require_email_verification and not user.email_verified:
        raise HTTPException(403, "Verify your email before continuing")
    return user


Actor = Annotated[User, Depends(current_user)]


def staff_only(user: User):
    if user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Staff access required")
