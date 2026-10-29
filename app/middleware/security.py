"""Bounded request bodies, distributed throttling and redacted request telemetry."""

import hashlib
import json
import logging
import secrets
import time
from collections import OrderedDict, deque

from prometheus_client import Counter, Histogram
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.middleware.proxy import restore_client

logger = logging.getLogger("affiliate.requests")
requests_total = Counter(
    "affiliate_http_requests_total", "HTTP responses", ["method", "route", "status"]
)
duration = Histogram("affiliate_http_seconds", "Request duration", ["route"])
buckets = OrderedDict()
HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"}
LUA_LIMIT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return current
"""


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app
        self.settings = get_settings()
        self.redis = (
            Redis.from_url(self.settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
            if self.settings.redis_url
            else None
        )

    async def allowed(self, identity, group, limit):
        key = "rate:" + group + ":" + hashlib.sha256(identity.encode()).hexdigest()
        if self.redis:
            return await self.redis.eval(LUA_LIMIT, 1, key, 60) <= limit
        now = time.monotonic()
        bucket = buckets.setdefault(key, deque())
        buckets.move_to_end(key)
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        while len(buckets) > 10000:
            buckets.popitem(last=False)
        return True

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            try:
                return await self.app(scope, receive, send)
            finally:
                if scope["type"] == "lifespan" and self.redis:
                    await self.redis.aclose()
        started = time.monotonic()
        request_id = secrets.token_hex(16)
        scope.setdefault("state", {})["request_id"] = request_id
        response_status = 500
        response_started = False

        async def secure_send(message):
            nonlocal response_status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                response_status = message["status"]
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-request-id", request_id.encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"cache-control", b"no-store"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    ]
                )
                if self.settings.environment in {"staging", "production"}:
                    headers.append(
                        (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
                    )
                if not scope["path"].startswith(("/docs", "/openapi.json")):
                    headers.append(
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
                        )
                    )
                message["headers"] = headers
            await send(message)

        async def reject(status, detail):
            response = JSONResponse(
                {"detail": detail, "request_id": request_id},
                status_code=status,
                headers={"Retry-After": "60"} if status == 429 else None,
            )
            await response(scope, receive, secure_send)

        try:
            try:
                scope = restore_client(scope, self.settings.proxy_ip_secret)
            except ValueError:
                return await reject(400, "Invalid proxy identity")
            path = scope["path"]
            exempt = path in {"/health", "/health/ready", "/metrics"}
            if not exempt:
                identity = (scope.get("client") or ("unknown", 0))[0]
                group = (
                    "auth"
                    if path.startswith("/auth/")
                    else "tracking"
                    if path == "/c"
                    or path.startswith(
                        (
                            "/t/",
                            "/i/",
                            "/capture/",
                            "/s/",
                            "/c/",
                            "/postbacks/",
                            "/pb/",
                            "/billing/webhook",
                        )
                    )
                    else "api"
                )
                limit = {
                    "auth": self.settings.auth_rate_per_minute,
                    "tracking": self.settings.tracking_rate_per_minute,
                    "api": self.settings.rate_limit_per_minute,
                }[group]
                try:
                    if not await self.allowed(identity, group, limit):
                        return await reject(429, "Rate limit exceeded")
                except RedisError:
                    return await reject(503, "Rate limiting service unavailable")
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > self.settings.max_request_bytes:
                    return await reject(413, "Request body too large")
                if not message.get("more_body", False):
                    break
            if path == "/auth/login":
                try:
                    payload = json.loads(body)
                    identity = str(payload.get("email", "")).strip().lower()
                    if not await self.allowed(identity, "account", 10):
                        return await reject(429, "Account login rate limit exceeded")
                except (ValueError, AttributeError):
                    pass
                except RedisError:
                    return await reject(503, "Rate limiting service unavailable")
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, replay, secure_send)
        except Exception as error:
            # Prevent upstream server logging SQL exception parameters and other sensitive values.
            logger.error("request_failed type=%s request_id=%s", type(error).__name__, request_id)
            if not response_started:
                await reject(500, "Internal server error")
            else:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            elapsed = time.monotonic() - started
            method = scope["method"] if scope["method"] in HTTP_METHODS else "OTHER"
            requests_total.labels(method, route, str(response_status)).inc()
            duration.labels(route).observe(elapsed)
            logger.info(
                json.dumps(
                    {
                        "request_id": request_id,
                        "method": method,
                        "route": route,
                        "status": response_status,
                        "duration_ms": round(elapsed * 1000, 2),
                    }
                )
            )
