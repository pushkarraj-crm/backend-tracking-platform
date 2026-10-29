import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis
from sqlalchemy import text

from app.api.router import router
from app.core.config import get_settings
from app.core.database import engine
from app.middleware.company import CompanyMiddleware
from app.middleware.domains import DomainHostMiddleware
from app.middleware.security import SecurityMiddleware

settings = get_settings()
schema_head = ScriptDirectory.from_config(
    Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
).get_current_head()
logging.basicConfig(level=logging.INFO, format="%(message)s")


@asynccontextmanager
async def lifespan(app):
    if settings.geoip_database_path:
        from app.features.tracking.rules import geo_reader

        geo_reader()
    yield
    engine.dispose()


app = FastAPI(
    title="Affiliate Network Platform",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.environment != "production" else None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
    expose_headers=["X-Request-ID", "X-Export-Rows", "X-Export-Max-Rows", "X-Export-Truncated"],
    allow_credentials=True,
)
app.add_middleware(DomainHostMiddleware)
app.add_middleware(CompanyMiddleware)
app.add_middleware(SecurityMiddleware)
app.include_router(router)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error):
    return JSONResponse(
        {
            "detail": [
                {"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in error.errors()
            ],
            "request_id": request.state.request_id,
        },
        status_code=422,
    )


@app.exception_handler(Exception)
async def internal_error(request: Request, error):
    logging.getLogger("affiliate.errors").error(
        "Unhandled %s request_id=%s",
        type(error).__name__,
        getattr(request.state, "request_id", "unknown"),
    )
    return JSONResponse(
        {
            "detail": "Internal server error",
            "request_id": getattr(request.state, "request_id", None),
        },
        status_code=500,
    )


@app.get("/health/ready", include_in_schema=False)
async def ready():
    from starlette.concurrency import run_in_threadpool

    try:

        def database_probe():
            from app.tenancy.service import company_context, company_sessions

            company = company_context.get()
            target = company_sessions(company).kw["bind"] if company else engine
            with target.connect() as connection:
                connection.execute(text("SELECT 1"))
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
                if revision != schema_head:
                    raise RuntimeError("Migration required")

        await run_in_threadpool(database_probe)
        if settings.redis_url:
            async with Redis.from_url(
                settings.redis_url, socket_connect_timeout=2, socket_timeout=2
            ) as client:
                await client.ping()
    except Exception:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return {"status": "ready"}


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    token = request.headers.get("authorization", "")
    if not settings.metrics_token or not secrets.compare_digest(
        token, "Bearer " + settings.metrics_token
    ):
        raise HTTPException(401, "Metrics authentication required")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", include_in_schema=False)
def index():
    return {
        "service": "Affiliate Network API",
        "version": app.version,
        "docs": "/docs" if settings.environment != "production" else None,
    }
