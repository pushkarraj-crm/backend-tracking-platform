import re
from urllib.parse import parse_qs, urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.core.database import ControlSession
from app.persistence.tenancy import CompanyHostname
from app.tenancy.service import find_company, using_company


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
            r"/c/([a-z0-9-]{1,63})(/c|/(?:t/|i/|capture/|s/|l/|postbacks/|pb/|wc/|billing/).*)",
            path,
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
            or scope["path"].startswith(("/t/", "/i/", "/capture/", "/s/", "/l/", "/wc/"))
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
