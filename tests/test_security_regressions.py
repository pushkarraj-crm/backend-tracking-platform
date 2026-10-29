"""Regressions for traffic policy, proxy trust, metrics and restricted staff."""

import hashlib
import hmac
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from test_api import client as api_client  # noqa: F401 -- shared isolated fixture
from test_campaign_invoices import campaign_flow, partner

from app.core.config import get_settings
from app.middleware.proxy import restore_client
from app.middleware.security import SecurityMiddleware, buckets, requests_total

KEY = "proxy-test-secret-" * 3


@pytest.fixture
def client(api_client):  # noqa: F811
    return api_client


@pytest.mark.parametrize("policy", ["source_block", "campaign_rule"])
def test_duplicate_click_rechecks_current_traffic_policy(client, policy):
    admin, adv, pub, _, _, campaign, link, original = campaign_flow(client)
    assert (
        client.put(
            f"/offers/{campaign['id']}/rules", headers=adv, json={"unique_click_hours": 24}
        ).status_code
        == 200
    )
    if policy == "source_block":
        response = client.post(
            "/blocked-sources",
            headers=admin,
            json={"preset": "custom_source", "value": "blocked-source"},
        )
    else:
        response = client.post(
            f"/campaigns/{campaign['id']}/traffic-rules",
            headers=adv,
            json={
                "name": "Block source",
                "phase": "click",
                "action": "reject",
                "conditions": {"all": [{"field": "source", "value": "blocked-source"}]},
            },
        )
    assert response.status_code == 201, response.text
    assert (
        client.get(f"/t/{link['id']}?source=blocked-source", follow_redirects=False).status_code
        == 403
    )
    allowed = client.get(f"/t/{link['id']}?source=allowed-source", follow_redirects=False)
    assert allowed.status_code == 302
    assert parse_qs(urlsplit(allowed.headers["location"]).query)["click_id"] == [original]
    clicks = client.get("/reports/clicks", headers=pub).json()
    assert len(clicks) == 1 and clicks[0]["attributes"].get("source") is None


def test_unknown_methods_use_one_metric_label(client):
    for method in ["SECURITYREVIEWONE", "SECURITYREVIEWTWO"]:
        assert client.request(method, "/health").status_code == 405
    methods = {key[0] for key in requests_total._metrics}
    assert "OTHER" in methods
    assert not methods & {"SECURITYREVIEWONE", "SECURITYREVIEWTWO"}


def test_staff_new_feature_reads_and_writes_require_their_capabilities(client):
    admin, adv, pub, _, _, campaign, _, _ = campaign_flow(client)
    staff, headers = partner(client, admin, "staff", "securitystaff")
    path = f"/users/{staff['id']}/permissions"
    assert client.put(path, headers=admin, json={"permissions": []}).status_code == 200
    for endpoint in ["/targets", "/blocked-sources"]:
        assert client.get(endpoint, headers=headers).status_code == 403
    assert (
        client.put(
            path, headers=admin, json={"permissions": ["campaigns:read", "fraud:read"]}
        ).status_code
        == 200
    )
    for endpoint in ["/targets", f"/campaigns/{campaign['id']}/targets", "/blocked-sources"]:
        assert client.get(endpoint, headers=headers).status_code == 200
    body = {"preset": "custom_source", "value": "blocked-source"}
    assert client.post("/blocked-sources", headers=headers, json=body).status_code == 403
    assert (
        client.put(
            path, headers=admin, json={"permissions": ["fraud:read", "fraud:review"]}
        ).status_code
        == 200
    )
    created = client.post("/blocked-sources", headers=headers, json=body)
    assert created.status_code == 201
    assert (
        client.patch(
            f"/blocked-sources/{created.json()['id']}", headers=headers, json={"active": False}
        ).status_code
        == 200
    )
    assert client.get("/targets", headers=headers).status_code == 403


def signed_scope(ip="203.0.113.10", timestamp="1800000000"):
    target = "/auth/login?next=a+b"
    message = "\n".join(["relay-ip-v1", timestamp, ip, "POST", target, "demo"])
    signature = hmac.new(KEY.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {
        "type": "http",
        "method": "POST",
        "path": "/auth/login",
        "raw_path": b"/auth/login",
        "query_string": b"next=a+b",
        "client": ("192.0.2.1", 443),
        "headers": [
            (b"x-relay-client-ip", ip.encode()),
            (b"x-relay-proxy-time", timestamp.encode()),
            (b"x-relay-proxy-signature", signature.encode()),
            (b"x-company-slug", b"demo"),
        ],
    }


def test_proxy_signature_matches_node_protocol_vector_and_does_not_mutate_scope(monkeypatch):
    monkeypatch.setattr("app.middleware.proxy.time.time", lambda: 1800000000)
    scope = signed_scope()
    assert (
        dict(scope["headers"])[b"x-relay-proxy-signature"]
        == b"b9859815436896fc85c26900995cda50ebf30dd6b96917324cc0ae94407f4516"
    )
    assert restore_client(scope, KEY)["client"] == ("203.0.113.10", 0)
    assert scope["client"] == ("192.0.2.1", 443)


@pytest.mark.parametrize(
    "tamper",
    [
        "signature",
        "ip",
        "timestamp",
        "expired",
        "method",
        "path",
        "query",
        "company",
        "missing",
        "duplicate",
        "unconfigured",
    ],
)
def test_proxy_rejects_forged_expired_or_rebound_identity(monkeypatch, tamper):
    monkeypatch.setattr("app.middleware.proxy.time.time", lambda: 1800000000)
    scope = signed_scope(timestamp="1799999939" if tamper == "expired" else "1800000000")
    header = {
        "signature": b"x-relay-proxy-signature",
        "ip": b"x-relay-client-ip",
        "timestamp": b"x-relay-proxy-time",
        "company": b"x-company-slug",
    }.get(tamper)
    if header:
        scope["headers"] = [(k, b"forged" if k == header else v) for k, v in scope["headers"]]
    if tamper == "method":
        scope["method"] = "GET"
    elif tamper == "path":
        scope["raw_path"] = b"/auth/refresh"
    elif tamper == "query":
        scope["query_string"] = b"next=changed"
    elif tamper == "missing":
        scope["headers"] = scope["headers"][1:]
    elif tamper == "duplicate":
        scope["headers"].append(scope["headers"][0])
    with pytest.raises(ValueError):
        restore_client(scope, "" if tamper == "unconfigured" else KEY)


def test_unsigned_forwarded_headers_cannot_replace_the_connection_identity():
    scope = signed_scope()
    scope["headers"] = [(b"x-forwarded-for", b"203.0.113.10"), (b"x-real-ip", b"203.0.113.10")]
    assert restore_client(scope, KEY) is scope


def test_authenticated_proxy_identity_controls_ip_rate_limits(monkeypatch):
    import time

    settings = get_settings()
    monkeypatch.setattr(settings, "proxy_ip_secret", KEY)
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(settings, "auth_rate_per_minute", 1)
    buckets.clear()

    async def endpoint(request):
        return JSONResponse({"ip": request.client.host})

    service = Starlette(routes=[Route("/auth/login", endpoint, methods=["POST"])])
    with TestClient(SecurityMiddleware(service)) as browser:

        def headers(ip):
            return {
                k.decode(): v.decode()
                for k, v in signed_scope(ip, str(int(time.time())))["headers"]
            }

        first = browser.post("/auth/login?next=a+b", headers=headers("203.0.113.10"))
        assert first.status_code == 200 and first.json()["ip"] == "203.0.113.10"
        assert (
            browser.post("/auth/login?next=a+b", headers=headers("203.0.113.10")).status_code == 429
        )
        other = browser.post("/auth/login?next=a+b", headers=headers("198.51.100.20"))
        assert other.status_code == 200 and other.json()["ip"] == "198.51.100.20"
        assert (
            browser.post(
                "/auth/login?next=a+b", headers={"x-relay-client-ip": "127.0.0.1"}
            ).status_code
            == 400
        )
