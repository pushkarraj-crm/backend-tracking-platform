import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
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
from .models import TrackingDomain, Workspace, User
from .schemas import StrictModel
from .security import DB, Actor, key_hash
from .services import audit, utc, entitlement
from .db import SessionLocal

router = APIRouter(tags=["Tracking domains"])


class DomainBody(StrictModel):
    hostname: str = Field(min_length=4, max_length=253)
    advertiser_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    purpose: Literal["tracking", "landing", "both"] = "tracking"

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
    if user.role not in {"admin", "advertiser"}:
        raise HTTPException(403, "Advertiser or admin access required")
    from .routes import owned

    advertiser_id = user.id if user.role == "advertiser" else body.advertiser_id
    if user.role == "advertiser" and body.advertiser_id not in {None, user.id}:
        raise HTTPException(403, "Cannot register another advertiser's domain")
    if advertiser_id:
        advertiser = owned(db, User, advertiser_id, user)
        if advertiser.role != "advertiser" or not advertiser.active:
            raise HTTPException(422, "Select an active advertiser")
    if body.purpose != "tracking" and not advertiser_id:
        raise HTTPException(422, "Landing domains require an advertiser")
    entitlement(db.get(Workspace, user.workspace_id))
    if not get_settings().tracking_cname:
        raise HTTPException(503, "Tracking DNS target is not configured")
    raw = secrets.token_urlsafe(32)
    domain = TrackingDomain(
        workspace_id=user.workspace_id,
        hostname=body.hostname,
        verification_hash=key_hash(raw),
        advertiser_id=advertiser_id,
        purpose=body.purpose,
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

    domain = owned(db, TrackingDomain, domain_id, user)
    if user.role != "admin" and not (user.role == "advertiser" and domain.advertiser_id == user.id):
        raise HTTPException(403, "Domain owner or admin access required")
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
    if user.role not in {"admin", "staff", "advertiser"}:
        raise HTTPException(403, "Staff access required")
    return [
        {
            "id": d.id,
            "hostname": d.hostname,
            "advertiser_id": d.advertiser_id,
            "purpose": d.purpose,
            "https_status": "verified"
            if d.tls_verified_at
            and utc(d.tls_verified_at) > datetime.now(timezone.utc) - timedelta(days=1)
            else "verification_required",
            "tls_verified_at": d.tls_verified_at,
            "active": enabled(d),
            "verified_at": d.verified_at,
            "expires_at": d.verification_expires_at,
        }
        for d in db.scalars(
            select(TrackingDomain)
            .where(
                TrackingDomain.workspace_id == user.workspace_id,
                TrackingDomain.advertiser_id == user.id if user.role == "advertiser" else True,
            )
            .order_by(TrackingDomain.hostname)
            .limit(100)
        )
    ]


def check_tls(hostname):
    """Resolve once, reject non-public addresses, and verify the original TLS name."""
    import ipaddress
    import socket
    import ssl

    addresses = {
        entry[4][0] for entry in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    }
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Domain must resolve only to public addresses")
    with socket.create_connection((sorted(addresses)[0], 443), timeout=10) as connection:
        with ssl.create_default_context().wrap_socket(connection, server_hostname=hostname):
            pass


@router.post("/tracking-domains/{domain_id}/verify-https")
def verify_https(domain_id: str, db: DB, user: Actor):
    from .routes import owned

    domain = owned(db, TrackingDomain, domain_id, user)
    if user.role != "admin" and not (user.role == "advertiser" and domain.advertiser_id == user.id):
        raise HTTPException(403, "Domain owner or admin access required")
    if not enabled(domain):
        raise HTTPException(422, "Verify DNS ownership before checking HTTPS")
    try:
        check_tls(domain.hostname)
    except (OSError, ValueError):
        domain.tls_verified_at = None
        audit(db, user, "domain.https_failed", domain.id)
        db.commit()
        raise HTTPException(
            422, "HTTPS certificate is not ready. Check gateway and DNS configuration."
        ) from None
    domain.tls_verified_at = datetime.now(timezone.utc)
    audit(db, user, "domain.https_verified", domain.id)
    db.commit()
    return {"id": domain.id, "https_status": "verified", "verified_at": domain.tls_verified_at}


@router.delete("/tracking-domains/{domain_id}")
def disable_domain(domain_id: str, db: DB, user: Actor):
    from .routes import owned

    domain = owned(db, TrackingDomain, domain_id, user)
    if user.role != "admin" and not (user.role == "advertiser" and domain.advertiser_id == user.id):
        raise HTTPException(403, "Domain owner or admin access required")
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


def enforce_tracking_host(db, request, workspace_id, advertiser_id=None):
    hostname = request.url.hostname
    if hostname in get_settings().allowed_hosts:
        return
    domain = db.scalar(select(TrackingDomain).where(TrackingDomain.hostname == hostname))
    if (
        not enabled(domain)
        or domain.workspace_id != workspace_id
        or domain.purpose not in {"tracking", "both"}
        or (domain.advertiser_id and domain.advertiser_id != advertiser_id)
    ):
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
        if scope["path"] == "/c" or scope["path"].startswith(
            ("/t/", "/i/", "/capture/", "/s/", "/l/")
        ):

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


def link_base(db, user, domain_id, advertiser_id=None):
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
    if (
        not enabled(record)
        or record.purpose not in {"tracking", "both"}
        or (record.advertiser_id and record.advertiser_id != advertiser_id)
    ):
        raise HTTPException(404, "Verified tracking domain not found")
    return "https://" + record.hostname + suffix
