"""Company directory, database provisioning and request/worker routing.

Directory credentials never leave the control database. Tenant selection is a
routing hint, not authorization: normal sessions and workspace ownership still
apply inside the selected database.
"""

from contextvars import ContextVar
from contextlib import contextmanager
from functools import lru_cache
import logging
import re
import secrets
import unicodedata
import uuid
import threading
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi import HTTPException
from psycopg import sql
from sqlalchemy import String, Text, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import NullPool
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .config import get_settings
from .db import Base, ControlSession

company_context = ContextVar("company", default=None)
migration_lock = threading.Lock()
reserved = {"www", "api", "admin", "app", "mail", "support", "status", "billing", "localhost"}


class Company(Base):
    __tablename__ = "company_directory"
    slug: Mapped[str] = mapped_column(String(63), primary_key=True)
    database_name: Mapped[str] = mapped_column(String(63), unique=True)
    database_secret: Mapped[str] = mapped_column(Text)
    workspace_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="provisioning", index=True)


class CompanyHostname(Base):
    __tablename__ = "company_hostnames"
    hostname: Mapped[str] = mapped_column(String(253), primary_key=True)
    slug: Mapped[str] = mapped_column(String(63), index=True)


def reserve_hostname(hostname):
    company = company_context.get()
    if not company:
        return
    from sqlalchemy.exc import IntegrityError

    try:
        with ControlSession.begin() as db:
            record = db.get(CompanyHostname, hostname)
            if record and record.slug != company.slug:
                raise HTTPException(409, "Tracking hostname belongs to another company")
            if not record:
                db.add(CompanyHostname(hostname=hostname, slug=company.slug))
    except IntegrityError:
        raise HTTPException(409, "Tracking hostname is already registered") from None


def company_slug(name):
    first = name.strip().split()[0] if name.strip() else "company"
    first = unicodedata.normalize("NFKD", first).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"[^a-z0-9]+", "-", first).strip("-")[:45].strip("-") or "company"
    return "company-" + value if value in reserved else value


def dashboard_url(slug):
    settings = get_settings()
    local = settings.company_base_domain == "localhost"
    return f"{'http' if local else 'https'}://{slug}.{settings.company_base_domain}" + (
        f":{settings.company_frontend_port}" if local else ""
    )


@contextmanager
def using_company(company):
    token = company_context.set(company)
    try:
        yield
    finally:
        company_context.reset(token)


def company_sessions(company):
    return sessions_for_secret(company.database_secret)


@lru_cache(maxsize=32)
def sessions_for_secret(secret):
    # NullPool bounds idle connection/memory usage as the number of companies grows.
    url = Fernet(get_settings().encryption_key.encode()).decrypt(secret.encode()).decode()
    engine = create_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=10000 -c timezone=UTC",
        },
    )
    return sessionmaker(engine, expire_on_commit=False)


def find_company(slug):
    with ControlSession() as db:
        return db.scalar(select(Company).where(Company.slug == slug, Company.status == "active"))


def active_companies():
    with ControlSession() as db:
        return list(
            db.scalars(select(Company).where(Company.status == "active").order_by(Company.slug))
        )


def for_each_company(callback):
    if not get_settings().company_databases:
        return callback()
    result = False
    for company in active_companies():
        try:
            with using_company(company):
                result = bool(callback()) or result
        except Exception as error:
            logging.getLogger("affiliate.tenancy").error(
                "Company task failed slug=%s error=%s", company.slug, type(error).__name__
            )
    return result


def upgrade_database(factory):
    # Alembic's environment proxy is process-global; serialize concurrent signups.
    with migration_lock, factory.kw["bind"].connect() as connection:
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def provision(body, create_account):
    settings = get_settings()
    name = "company_" + uuid.uuid4().hex
    password = secrets.token_urlsafe(40)
    admin_url = make_url(settings.company_database_admin_url or settings.database_url)
    tenant_url = admin_url.set(username=name, password=password, database=name)
    encrypted = (
        Fernet(settings.encryption_key.encode())
        .encrypt(tenant_url.render_as_string(hide_password=False).encode())
        .decode()
    )
    with ControlSession.begin() as db:
        # Serialize reservations; a unique PK also enforces this across processes.
        db.execute(text("SELECT pg_advisory_xact_lock(721903417)"))
        base = company_slug(body.company)
        slug, suffix = base, 1
        while db.get(Company, slug):
            suffix += 1
            slug = f"{base}-{suffix}"
        company = Company(
            slug=slug, database_name=name, database_secret=encrypted, status="provisioning"
        )
        db.add(company)
    admin = create_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool, hide_parameters=True
    )
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE")
                .format(sql.Identifier(name), sql.Literal(password))
                .as_string()
            )
            connection.exec_driver_sql(
                sql.SQL("GRANT {} TO CURRENT_USER WITH SET TRUE")
                .format(sql.Identifier(name))
                .as_string()
            )
            connection.exec_driver_sql(
                sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0")
                .format(sql.Identifier(name), sql.Identifier(name))
                .as_string()
            )
            connection.exec_driver_sql(
                sql.SQL("SET ROLE {}").format(sql.Identifier(name)).as_string()
            )
            try:
                connection.exec_driver_sql(
                    sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC")
                    .format(sql.Identifier(name))
                    .as_string()
                )
            finally:
                connection.exec_driver_sql("RESET ROLE")
            connection.exec_driver_sql(
                sql.SQL("REVOKE {} FROM CURRENT_USER").format(sql.Identifier(name)).as_string()
            )
        factory = company_sessions(company)
        upgrade_database(factory)
        with using_company(company), factory() as db:
            create_account(body, db)
            from .models import Workspace

            workspace_id = db.scalar(select(Workspace.id))
        with ControlSession.begin() as db:
            db.get(Company, slug).status = "active"
            db.get(Company, slug).workspace_id = workspace_id
    except Exception as error:
        with ControlSession.begin() as db:
            db.get(Company, slug).status = "failed"
        logging.getLogger("affiliate.tenancy").error(
            "Company provisioning failed slug=%s error=%s", slug, type(error).__name__
        )
        raise HTTPException(503, "Company setup failed. Contact support before retrying.") from None
    finally:
        admin.dispose()
    return {
        "company_slug": slug,
        "dashboard_url": dashboard_url(slug),
        "login_required": True,
        "trial_days": 15,
    }


class CompanyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        settings = get_settings()
        if scope["type"] != "http" or not settings.company_databases:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        slug = headers.get(b"x-company-slug", b"").decode("ascii", errors="ignore")
        path = scope["path"]
        if path == "/internal/certificates/allow" and not slug:
            domain = parse_qs(scope.get("query_string", b"").decode()).get("domain", [""])[0]

            def certificate_company():
                with ControlSession() as db:
                    record = db.get(CompanyHostname, domain.lower())
                    return record.slug if record else ""

            slug = await run_in_threadpool(certificate_company)
        # Public S2S callbacks and tracking links have an explicit company prefix.
        match = re.fullmatch(
            r"/c/([a-z0-9-]{1,63})(/c|/(?:t/|i/|capture/|s/|l/|postbacks/|pb/|billing/).*)", path
        )
        if match:
            if slug and slug != match[1]:
                return await JSONResponse({"detail": "Company mismatch"}, 400)(scope, receive, send)
            slug = match[1]
            scope = dict(scope, path=match[2], raw_path=match[2].encode())
        try:
            host = (
                urlsplit("//" + headers.get(b"host", b"").decode("ascii", errors="ignore")).hostname
                or ""
            )
        except ValueError:
            return await JSONResponse({"detail": "Invalid host"}, 400)(scope, receive, send)
        if not slug and (
            scope["path"] == "/c"
            or scope["path"].startswith(("/t/", "/i/", "/capture/", "/s/", "/l/"))
        ):

            def public_domain_company():
                with ControlSession() as db:
                    record = db.get(CompanyHostname, host)
                    return record.slug if record else ""

            slug = await run_in_threadpool(public_domain_company)
        suffix = "." + settings.company_base_domain
        host_slug = host[: -len(suffix)] if host.endswith(suffix) else ""
        if host_slug:
            if slug and slug != host_slug:
                return await JSONResponse({"detail": "Company mismatch"}, 400)(scope, receive, send)
            slug = host_slug
        public = path in {
            "/auth/register",
            "/auth/registration-options",
            "/auth/login",
            "/auth/refresh",
            "/auth/csrf",
            "/auth/logout",
            "/me",
            "/billing/webhook",
            "/health",
            "/health/ready",
            "/openapi.json",
            "/docs",
            "/metrics",
            "/internal/certificates/allow",
            "/",
            "/sdk/relay.js",
        }
        if slug and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            return await JSONResponse({"detail": "Invalid company"}, 400)(scope, receive, send)
        company = await run_in_threadpool(find_company, slug) if slug else None
        if (slug and not company) or (
            not slug and not public and not path.startswith("/platform/")
        ):
            return await JSONResponse({"detail": "Open your company dashboard to continue"}, 404)(
                scope, receive, send
            )
        with using_company(company):
            await self.app(scope, receive, send)


def migrate_companies():
    if not get_settings().company_databases:
        return
    for company in active_companies():
        upgrade_database(company_sessions(company))
