from urllib.parse import urlsplit

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.features.domains.policy import enabled
from app.persistence.models import TrackingDomain


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
        from app.tenancy.service import company_context, dashboard_url

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
