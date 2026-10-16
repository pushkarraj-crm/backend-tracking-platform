import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import dns.resolver
import idna
from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from .config import get_settings
from .models import TrackingDomain, Workspace
from .schemas import StrictModel
from .security import DB, Actor, key_hash
from .services import audit, utc, entitlement
from .db import SessionLocal

router = APIRouter(tags=["Tracking domains"])


class DomainBody(StrictModel):
    hostname: str = Field(min_length=4, max_length=253)

    @field_validator("hostname")
    @classmethod
    def canonical(cls, value):
        if any(c in value for c in "/:@*") or value.endswith("."):
            raise ValueError("Supply a hostname without scheme, path, port or wildcard")
        try:
            value = idna.encode(value.lower(), uts46=True).decode()
        except idna.IDNAError as exc:
            raise ValueError("Supply a valid DNS hostname") from exc
        if len(value.split(".")) < 2 or value in get_settings().allowed_hosts:
            raise ValueError("Use a dedicated tracking subdomain")
        return value


def enabled(domain):
    return bool(
        domain
        and domain.active
        and domain.verification_expires_at
        and utc(domain.verification_expires_at) > datetime.now(timezone.utc)
    )


@router.post("/tracking-domains", status_code=201)
def create_domain(body: DomainBody, db: DB, user: Actor):
    if user.role != "admin":
        raise HTTPException(403, "Workspace admin required")
    entitlement(db.get(Workspace, user.workspace_id))
    if not get_settings().tracking_cname:
        raise HTTPException(503, "Tracking DNS target is not configured")
    raw = secrets.token_urlsafe(32)
    domain = TrackingDomain(
        workspace_id=user.workspace_id, hostname=body.hostname, verification_hash=key_hash(raw)
    )
    db.add(domain)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Tracking domain already registered")
    audit(db, user, "domain.created", domain.id)
    db.commit()
    return {
        "id": domain.id,
        "hostname": domain.hostname,
        "txt_name": "_affiliate-verify." + domain.hostname,
        "txt_value": raw,
        "cname_target": get_settings().tracking_cname,
    }


def verify_dns(domain):
    resolver = dns.resolver.Resolver()
    resolver.timeout = 2
    resolver.lifetime = 5
    try:
        txt = resolver.resolve("_affiliate-verify." + domain.hostname, "TXT")
        tokens = [b"".join(record.strings).decode("ascii") for record in txt]
        cname = resolver.resolve(domain.hostname, "CNAME")
        names = [str(record.target).rstrip(".").lower() for record in cname]
    except Exception:
        return False
    return (
        any(secrets.compare_digest(domain.verification_hash, key_hash(token)) for token in tokens)
        and get_settings().tracking_cname.rstrip(".").lower() in names
    )


@router.post("/tracking-domains/{domain_id}/verify")
def verify_domain(domain_id: str, db: DB, user: Actor):
    from .routes import owned

    if user.role != "admin":
        raise HTTPException(403, "Workspace admin required")
    domain = owned(db, TrackingDomain, domain_id, user)
    if not verify_dns(domain):
        raise HTTPException(422, "DNS ownership TXT and routing CNAME could not be verified")
    from .tenancy import reserve_hostname

    reserve_hostname(domain.hostname)
    domain.active = True
    domain.verified_at = datetime.now(timezone.utc)
    domain.verification_expires_at = domain.verified_at + timedelta(days=7)
    audit(db, user, "domain.verified", domain.id)
    db.commit()
    return {"id": domain.id, "verified": True, "expires_at": domain.verification_expires_at}


@router.get("/tracking-domains")
def list_domains(db: DB, user: Actor):
    if user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Staff access required")
    return [
        {
            "id": d.id,
            "hostname": d.hostname,
            "active": enabled(d),
            "verified_at": d.verified_at,
            "expires_at": d.verification_expires_at,
        }
        for d in db.scalars(
            select(TrackingDomain)
            .where(TrackingDomain.workspace_id == user.workspace_id)
            .order_by(TrackingDomain.hostname)
            .limit(100)
        )
    ]


@router.delete("/tracking-domains/{domain_id}")
def disable_domain(domain_id: str, db: DB, user: Actor):
    from .routes import owned

    if user.role != "admin":
        raise HTTPException(403, "Workspace admin required")
    domain = owned(db, TrackingDomain, domain_id, user)
    domain.active = False
    audit(db, user, "domain.disabled", domain.id)
    db.commit()
    return {"disabled": True}


@router.get("/internal/certificates/allow", include_in_schema=False)
def certificate_permission(
    request: Request,
    db: DB,
    domain: str = Query(max_length=253),
    token: str = Query(max_length=256),
):
    expected = get_settings().domain_certificate_token
    if not expected or not secrets.compare_digest(expected, token):
        raise HTTPException(403, "Permission denied")
    if domain == urlsplit(get_settings().public_base_url).hostname:
        return {"allowed": True}
    record = db.scalar(select(TrackingDomain).where(TrackingDomain.hostname == domain.lower()))
    if not enabled(record) or not db.get(Workspace, record.workspace_id).active:
        raise HTTPException(403, "Domain is not verified")
    return {"allowed": True}


def enforce_tracking_host(db, request, workspace_id):
    hostname = request.url.hostname
    if hostname in get_settings().allowed_hosts:
        return
    domain = db.scalar(select(TrackingDomain).where(TrackingDomain.hostname == hostname))
    if not enabled(domain) or domain.workspace_id != workspace_id:
        raise HTTPException(404, "Tracking domain does not own this link")


class DomainHostMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        raw = headers.get(b"host", b"").decode("ascii", errors="ignore")
        try:
            hostname = urlsplit("//" + raw).hostname
        except ValueError:
            hostname = None
        if hostname in get_settings().allowed_hosts:
            return await self.app(scope, receive, send)
        from .tenancy import company_context, dashboard_url

        company = company_context.get()
        if company and hostname == urlsplit(dashboard_url(company.slug)).hostname:
            return await self.app(scope, receive, send)
        if (
            scope["path"] == "/internal/certificates/allow"
            and (scope.get("client") or ("", 0))[0] == get_settings().trusted_gateway_ip
        ):
            return await self.app(scope, receive, send)
        if scope["path"].startswith("/t/"):

            def permitted():
                with SessionLocal() as db:
                    record = db.scalar(
                        select(TrackingDomain).where(TrackingDomain.hostname == hostname)
                    )
                    return enabled(record)

            if await run_in_threadpool(permitted):
                return await self.app(scope, receive, send)
        return await JSONResponse({"detail": "Invalid host"}, status_code=400)(scope, receive, send)


def revalidate_domains():
    with SessionLocal() as db:
        records = list(
            db.scalars(
                select(TrackingDomain)
                .where(
                    TrackingDomain.active.is_(True),
                    TrackingDomain.verification_expires_at
                    < datetime.now(timezone.utc) + timedelta(days=1),
                )
                .limit(50)
            )
        )
        for record in records:
            if verify_dns(record):
                record.verification_expires_at = datetime.now(timezone.utc) + timedelta(days=7)
            else:
                record.active = False
        db.commit()


def link_base(db, user, domain_id):
    from .tenancy import company_context

    company = company_context.get()
    suffix = "/c/" + company.slug if company else ""
    if not domain_id:
        return get_settings().public_base_url.rstrip("/") + suffix
    record = db.scalar(
        select(TrackingDomain).where(
            TrackingDomain.id == domain_id, TrackingDomain.workspace_id == user.workspace_id
        )
    )
    if not enabled(record):
        raise HTTPException(404, "Verified tracking domain not found")
    return "https://" + record.hostname + suffix
