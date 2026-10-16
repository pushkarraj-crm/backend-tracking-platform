import secrets
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field, EmailStr
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from .config import get_settings
from .models import User, Workspace, AuthSession, ActionToken
from .schemas import StrictModel, Login, Register
from .security import DB, Actor, passwords, dummy_hash, token_for, key_hash
from .services import audit, enqueue, utc
from functools import lru_cache
import pycountry

router = APIRouter(prefix="/auth", tags=["Authentication"])
settings = get_settings()


class ActionBody(StrictModel):
    token: str = Field(min_length=32, max_length=200)


class ResetBody(ActionBody):
    password: str = Field(min_length=12, max_length=128)


class EmailAction(StrictModel):
    email: EmailStr


def issue_action(db, user, purpose):
    from .tenancy import company_context, dashboard_url

    company = company_context.get()
    raw = secrets.token_urlsafe(48)
    db.add(
        ActionToken(
            user_id=user.id,
            token_hash=key_hash(raw),
            purpose=purpose,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
    )
    enqueue(
        db,
        user.workspace_id,
        "email",
        {
            "to": user.email,
            "purpose": purpose,
            "url": (
                dashboard_url(company.slug) if company else settings.frontend_base_url.rstrip("/")
            )
            + "/account?action="
            + purpose
            + "#token="
            + raw,
        },
    )


def mint(db, user, response, session=None):
    if session is None:
        from .models import identifier

        session = AuthSession(
            workspace_id=user.workspace_id,
            user_id=user.id,
            id=identifier(),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_days),
        )
        db.add(session)
    raw = session.id + "." + secrets.token_urlsafe(48)
    session.refresh_hash = key_hash(raw)
    secure = settings.environment in {"staging", "production"}
    response.set_cookie(
        "refresh_token",
        raw,
        httponly=True,
        secure=secure,
        samesite=settings.cookie_samesite,
        path="/auth",
        max_age=settings.refresh_days * 86400,
    )
    csrf = secrets.token_urlsafe(32)
    response.set_cookie(
        "csrf_token",
        csrf,
        secure=secure,
        samesite=settings.cookie_samesite,
        path="/",
        max_age=settings.refresh_days * 86400,
    )
    db.flush()
    return {
        "access_token": token_for(user, session.id),
        "csrf_token": csrf,
        "token_type": "bearer",
        "expires_in": settings.token_minutes * 60,
        "user": {
            "id": user.id,
            "email": user.email,
            "role": user.role,
            "parent_user_id": user.parent_user_id,
        },
    }


def validate_origin(request):
    origin = request.headers.get("origin")
    from urllib.parse import urlsplit

    frontend = urlsplit(settings.frontend_base_url)
    allowed = [
        settings.public_base_url.rstrip("/"),
        f"{frontend.scheme}://{frontend.netloc}",
        *settings.cors_origins,
    ]
    from .tenancy import company_context, dashboard_url

    company = company_context.get()
    if company:
        allowed.append(dashboard_url(company.slug))
    if origin and origin not in allowed:
        raise HTTPException(403, "Origin not allowed")


def csrf_check(request):
    cookie = request.cookies.get("csrf_token", "")
    header = request.headers.get("x-csrf-token", "")
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise HTTPException(403, "Invalid CSRF token")
    validate_origin(request)


@router.get("/csrf")
def csrf_token(request: Request):
    validate_origin(request)
    token = request.cookies.get("csrf_token")
    if not token:
        raise HTTPException(401, "No browser session")
    return {"csrf_token": token}


@router.post("/register", status_code=201)
def register(body: Register, db: DB, response: Response):
    if not settings.allow_registration:
        raise HTTPException(403, "Registration is closed")
    if body.legal_version != settings.legal_version:
        raise HTTPException(
            422, "Policy version changed; reload the signup options and review the policies"
        )
    if settings.company_databases:
        from .tenancy import provision

        return provision(body, create_registered_account)
    return create_registered_account(body, db, response)


def create_registered_account(body, db, response=None):
    workspace = Workspace(
        name=body.company,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=15),
        business_type=body.business_type,
        usage_types=body.usage_types,
        country=body.country,
        mobile=body.mobile,
        teams_enabled=body.teams_enabled,
        teams_email=str(body.teams_email) if body.teams_email else None,
        preferred_language=body.preferred_language,
        referral_source=body.referral_source,
    )
    db.add(workspace)
    db.flush()
    user = User(
        workspace_id=workspace.id,
        email=str(body.email).lower(),
        password_hash=passwords.hash(body.password),
        role="admin",
        email_verified=not settings.require_email_verification,
        full_name=body.name,
        consent_version=settings.legal_version,
        consent_at=datetime.now(timezone.utc),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Email is already registered")
    audit(db, user, "workspace.created", workspace.id, "15-day trial")
    if settings.require_email_verification:
        issue_action(db, user, "verify")
        result = {"verification_required": True, "email": user.email, "trial_days": 15}
    elif response is not None:
        result = mint(db, user, response)
    else:
        result = {"login_required": True}
    db.commit()
    return result


@lru_cache
def country_options():
    return sorted(
        [{"value": c.alpha_2, "label": c.name} for c in pycountry.countries],
        key=lambda c: c["label"],
    )


@router.get("/registration-options")
def registration_options():
    from .schemas import Register

    schema = Register.model_json_schema()
    labels = {
        "brand_direct_advertiser": "Brand/Direct Advertiser",
        "agency": "Agency",
        "affiliate_ad_network": "Affiliate/Ad Network",
        "mobile_apps": "Mobile Apps",
        "media_buyer": "Media Buyer",
        "social_media_advertising": "Social Media Advertising",
        "cpi_app_promotion": "CPI & App Promotion",
        "lead_generation": "Lead Generation",
        "e_commerce": "E-Commerce",
        "email_marketing": "Email Marketing",
        "traffic_source": "Traffic Source",
        "finance_fintech": "Finance & Fintech",
        "travel_hospitality": "Travel & Hospitality",
        "en": "EN - English",
        "hi": "HI - Hindi",
        "es": "ES - Spanish",
        "fr": "FR - French",
        "ar": "AR - Arabic",
        "pt": "PT - Portuguese",
        "de": "DE - German",
    }
    result = {}
    for name in ("business_type", "usage_types", "preferred_language", "referral_source"):
        node = schema["properties"][name]
        values = node["items"]["enum"] if name == "usage_types" else node["enum"]
        result[name] = [
            {"value": v, "label": labels.get(v, v.replace("_", " ").title())} for v in values
        ]
    return {
        **result,
        "company_databases": settings.company_databases,
        "company_base_domain": settings.company_base_domain if settings.company_databases else None,
        "countries": country_options(),
        "registration_enabled": settings.allow_registration,
        "trial_days": 15,
        "credit_card_required": False,
        "setup_fee": False,
        "terms_url": settings.terms_url or None,
        "privacy_url": settings.privacy_url or None,
        "legal_version": settings.legal_version,
    }


@router.post("/login")
def login(body: Login, db: DB, response: Response):
    user = db.scalar(select(User).where(User.email == str(body.email).lower()))
    valid = passwords.verify(body.password, user.password_hash if user else dummy_hash)
    if not valid or not user or not user.active:
        raise HTTPException(401, "Invalid credentials")
    workspace = db.get(Workspace, user.workspace_id)
    if not workspace.active:
        raise HTTPException(403, "Workspace is suspended")
    if settings.require_email_verification and not user.email_verified:
        raise HTTPException(403, "Verify your email before login")
    result = mint(db, user, response)
    audit(db, user, "auth.login", user.id)
    db.commit()
    return result


@router.post("/refresh")
def refresh(request: Request, response: Response, db: DB):
    csrf_check(request)
    raw = request.cookies.get("refresh_token", "")
    session_id = raw.split(".")[0]
    session = db.scalar(select(AuthSession).where(AuthSession.id == session_id).with_for_update())
    if not session or session.revoked or utc(session.expires_at) <= datetime.now(timezone.utc):
        raise HTTPException(401, "Session expired")
    if not secrets.compare_digest(session.refresh_hash, key_hash(raw)):
        session.revoked = True
        db.commit()
        raise HTTPException(401, "Refresh token reuse detected; session revoked")
    user = db.get(User, session.user_id)
    if not user.active or not db.get(Workspace, user.workspace_id).active:
        raise HTTPException(401, "Account unavailable")
    if user.parent_user_id and not db.get(User, user.parent_user_id).active:
        raise HTTPException(401, "Parent affiliate unavailable")
    if settings.require_email_verification and not user.email_verified:
        raise HTTPException(403, "Email verification required")
    result = mint(db, user, response, session)
    db.commit()
    return result


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: DB):
    csrf_check(request)
    raw = request.cookies.get("refresh_token", "")
    db.execute(
        update(AuthSession)
        .where(AuthSession.id == raw.split(".")[0], AuthSession.refresh_hash == key_hash(raw))
        .values(revoked=True)
    )
    db.commit()
    response.delete_cookie("refresh_token", path="/auth")
    response.delete_cookie("csrf_token", path="/")


@router.post("/logout-all", status_code=204)
def logout_all(db: DB, user: Actor):
    user.auth_version += 1
    db.execute(update(AuthSession).where(AuthSession.user_id == user.id).values(revoked=True))
    audit(db, user, "auth.logout_all", user.id)
    db.commit()


@router.post("/verify")
def verify_email(body: ActionBody, db: DB):
    token = consume_action(db, body.token, "verify")
    user = db.get(User, token.user_id)
    user.email_verified = True
    audit(db, user, "auth.email_verified", user.id)
    db.commit()
    return {"verified": True}


def consume_action(db, raw, purpose):
    token = db.scalar(
        select(ActionToken)
        .where(ActionToken.token_hash == key_hash(raw), ActionToken.purpose == purpose)
        .with_for_update()
    )
    if not token or token.used or utc(token.expires_at) <= datetime.now(timezone.utc):
        raise HTTPException(400, "Invalid or expired action token")
    token.used = True
    return token


@router.post("/request-verification", status_code=202)
def request_verification(body: EmailAction, db: DB):
    return request_action(body, db, "verify")


@router.post("/forgot-password", status_code=202)
def forgot_password(body: EmailAction, db: DB):
    return request_action(body, db, "reset")


def request_action(body, db, purpose):
    if settings.environment == "staging" and not settings.smtp_host:
        raise HTTPException(
            503, "Email delivery is not configured for this personal test deployment"
        )
    user = db.scalar(select(User).where(User.email == str(body.email).lower()))
    if user and user.active and (purpose != "verify" or not user.email_verified):
        issue_action(db, user, purpose)
        db.commit()
    return {"message": "If the account is eligible, an email will be sent."}


@router.post("/reset-password")
def reset_password(body: ResetBody, db: DB):
    token = consume_action(db, body.token, "reset")
    user = db.get(User, token.user_id)
    user.password_hash = passwords.hash(body.password)
    user.auth_version += 1
    db.execute(update(AuthSession).where(AuthSession.user_id == user.id).values(revoked=True))
    audit(db, user, "auth.password_reset", user.id)
    db.commit()
    return {"reset": True}
