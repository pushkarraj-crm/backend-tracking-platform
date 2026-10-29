# ruff: noqa: E402 -- configure the isolated test database before importing the application
import os
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import pytest

test_directory = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "sqlite:///" + (Path(test_directory.name) / "test.db").as_posix()
)
os.environ["JWT_SECRET"] = "test-only-secret-" * 4
os.environ["AUTO_CREATE_TABLES"] = "false"
os.environ["ENVIRONMENT"] = "test"
os.environ["REDIS_URL"] = ""
os.environ["REQUIRE_EMAIL_VERIFICATION"] = "false"
os.environ["METRICS_TOKEN"] = "test-metrics-token-" * 3

from fastapi.testclient import TestClient
from sqlalchemy import select, event, text
from sqlalchemy.exc import IntegrityError
from app.db import Base, engine, SessionLocal
from app.main import app
from app.middleware import buckets
from app.models import TrackingLink, User

# PostgreSQL runs in a unique schema, so no application tables are dropped.
test_schema = None
if engine.dialect.name == "postgresql":
    import uuid

    test_schema = "test_" + uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{test_schema}"'))
    engine.dispose()

    @event.listens_for(engine, "connect")
    def configure_test_search_path(connection, _):
        previous = connection.autocommit
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{test_schema}"')
        connection.autocommit = previous


@pytest.fixture
def client():
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            # Fixture DDL is slower than normal API queries on developer machines.
            connection.execute(text("SET LOCAL statement_timeout = 60000"))
        Base.metadata.drop_all(connection)
        Base.metadata.create_all(connection)
    buckets.clear()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session", autouse=True)
def close_test_database():
    yield
    if test_schema:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{test_schema}" CASCADE'))
    engine.dispose()
    test_directory.cleanup()


def register(client, name="Network A"):
    response = client.post(
        "/auth/register",
        json={
            **signup_body(name),
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    with SessionLocal() as db:
        data["user"]["workspace_id"] = db.get(User, data["user"]["id"]).workspace_id
    return {"Authorization": "Bearer " + data["access_token"]}, data["user"]


def signup_body(company="Network A"):
    return {
        "name": "Network Admin",
        "company": company,
        "email": "admin+"
        + __import__("hashlib").sha256(company.encode()).hexdigest()[:12]
        + "@example.com",
        "password": "a-strong-password-123",
        "business_type": "affiliate_ad_network",
        "usage_types": ["lead_generation"],
        "country": "IN",
        "mobile": "+919876543210",
        "teams_enabled": False,
        "preferred_language": "en",
        "referral_source": "search_engine",
        "terms_accepted": True,
        "legal_version": "1",
    }


def setup_offer(client, headers):
    affiliate = client.post(
        "/users",
        headers=headers,
        json={
            "email": "affiliate@example.com",
            "password": "affiliate-password-123",
            "role": "affiliate",
        },
    ).json()
    offer = client.post(
        "/offers",
        headers=headers,
        json={
            "name": "Signup",
            "destination_url": "https://example.com/signup?campaign=test&click_id=old",
            "revenue": "10.5000",
            "payout": "7.2500",
        },
    ).json()
    link = client.post(
        "/links", headers=headers, json={"offer_id": offer["id"], "affiliate_id": affiliate["id"]}
    ).json()
    click_response = client.get("/t/" + link["id"], follow_redirects=False)
    assert click_response.status_code == 302
    query = parse_qs(urlsplit(click_response.headers["location"]).query)
    assert query["campaign"] == ["test"]
    assert len(query["click_id"]) == 1
    return affiliate, offer, link, query["click_id"][0]


def test_tracking_conversion_and_reports(client):
    headers, admin = register(client)
    affiliate, offer, link, click_id = setup_offer(client, headers)
    endpoint = "/postbacks/" + offer["id"]
    body = {"click_id": click_id, "transaction_id": "order-1"}
    assert client.post(endpoint, json=body, headers={"X-Postback-Key": "wrong"}).status_code == 401
    response = client.post(endpoint, json=body, headers={"X-Postback-Key": offer["postback_key"]})
    assert response.status_code == 201, response.text
    conversion_id = response.json()["id"]
    duplicate = client.post(endpoint, json=body, headers={"X-Postback-Key": offer["postback_key"]})
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True
    assert (
        client.post(
            endpoint,
            json={**body, "transaction_id": "order-2"},
            headers={"X-Postback-Key": offer["postback_key"]},
        ).status_code
        == 409
    )
    assert client.get("/reports/summary", headers=headers).json()["totals"] == []
    assert (
        client.patch(
            "/conversions/" + conversion_id, headers=headers, json={"status": "approved"}
        ).status_code
        == 200
    )
    report = client.get("/reports/summary", headers=headers).json()
    assert report["clicks"] == 1
    assert report["totals"][0]["profit"] == "3.2500"
    login = client.post(
        "/auth/login",
        json={
            "email": affiliate["email"],
            "password": "affiliate-password-123",
        },
    )
    affiliate_headers = {"Authorization": "Bearer " + login.json()["access_token"]}
    assert client.post("/offers", headers=affiliate_headers, json={}).status_code in {403, 422}
    assert client.get("/users", headers=affiliate_headers).json() == []
    assert (
        "revenue"
        not in client.get("/reports/summary", headers=affiliate_headers).json()["totals"][0]
    )


def test_tenant_isolation(client):
    headers_a, admin_a = register(client)
    affiliate, offer, link, click_id = setup_offer(client, headers_a)
    headers_b, admin_b = register(client, "Network B")
    assert client.get("/offers", headers=headers_b).json() == []
    assert client.get("/reports/summary", headers=headers_b).json()["clicks"] == 0
    assert (
        client.post(
            "/links",
            headers=headers_b,
            json={"offer_id": offer["id"], "affiliate_id": affiliate["id"]},
        ).status_code
        == 404
    )
    conversion = client.post(
        "/postbacks/" + offer["id"],
        json={"click_id": click_id, "transaction_id": "x"},
        headers={"X-Postback-Key": offer["postback_key"]},
    ).json()
    assert (
        client.patch(
            "/conversions/" + conversion["id"], headers=headers_b, json={"status": "approved"}
        ).status_code
        == 404
    )
    with SessionLocal() as db:
        db.add(
            TrackingLink(
                workspace_id=admin_b["workspace_id"],
                offer_id=offer["id"],
                affiliate_id=affiliate["id"],
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_auth_validation_and_rate_limit(client):
    headers, admin = register(client)
    assert client.get("/me").status_code == 401
    assert (
        client.get(
            "/me", headers={"Authorization": headers["Authorization"] + "tampered"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/login",
            json={
                "email": admin["email"],
                "password": "incorrect-password",
            },
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/offers",
            headers=headers,
            json={
                "name": "Unsafe",
                "destination_url": "javascript:alert(1)",
                "revenue": 1,
                "payout": 1,
            },
        ).status_code
        == 422
    )
    assert client.get("/offers?limit=100000", headers=headers).status_code == 422
    for _ in range(10):
        last = client.post(
            "/auth/login",
            json={
                "email": admin["email"],
                "password": "incorrect-password",
            },
        )
    assert last.status_code == 429
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.id == admin["id"]))
        user.active = False
        db.commit()
    assert client.get("/me", headers=headers).status_code == 401


def test_rotation_deactivation_and_audit(client):
    headers, admin = register(client)
    affiliate, offer, link, click_id = setup_offer(client, headers)
    rotated = client.post("/offers/" + offer["id"] + "/rotate-postback-key", headers=headers)
    assert rotated.status_code == 200
    body = {"click_id": click_id, "transaction_id": "rotated-order"}
    endpoint = "/postbacks/" + offer["id"]
    assert (
        client.post(
            endpoint, json=body, headers={"X-Postback-Key": offer["postback_key"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            endpoint, json=body, headers={"X-Postback-Key": rotated.json()["postback_key"]}
        ).status_code
        == 201
    )
    assert (
        client.patch("/offers/" + offer["id"], headers=headers, json={"active": False}).status_code
        == 200
    )
    assert client.get("/t/" + link["id"], follow_redirects=False).status_code == 410
    assert (
        client.patch(
            "/users/" + affiliate["id"], headers=headers, json={"active": False}
        ).status_code
        == 200
    )
    assert (
        client.patch("/users/" + admin["id"], headers=headers, json={"active": False}).status_code
        == 400
    )
    assert {"offer.rotate_key", "offer.active", "user.active"} <= {
        row["action"] for row in client.get("/audit-events", headers=headers).json()
    }


def test_cross_offer_postback_and_report_dates(client):
    headers, admin = register(client)
    affiliate, offer, link, click_id = setup_offer(client, headers)
    other = client.post(
        "/offers",
        headers=headers,
        json={
            "name": "Other",
            "destination_url": "https://example.org",
            "revenue": 2,
            "payout": 1,
            "currency": "INR",
        },
    ).json()
    assert (
        client.post(
            "/postbacks/" + other["id"],
            headers={"X-Postback-Key": other["postback_key"]},
            json={"click_id": click_id, "transaction_id": "bad-match"},
        ).status_code
        == 404
    )
    assert (
        client.get("/reports/summary?start=2026-01-01T00:00:00", headers=headers).status_code == 422
    )
    assert (
        client.get(
            "/reports/summary?start=2026-10-01T00:00:00Z&end=2026-01-01T00:00:00Z", headers=headers
        ).status_code
        == 422
    )


def test_concurrent_postback_retries(client):
    from concurrent.futures import ThreadPoolExecutor

    headers, admin = register(client)
    affiliate, offer, link, click_id = setup_offer(client, headers)

    def send():
        return client.post(
            "/postbacks/" + offer["id"],
            headers={"X-Postback-Key": offer["postback_key"]},
            json={"click_id": click_id, "transaction_id": "concurrent-order"},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: send(), range(4)))
    assert sorted(r.status_code for r in results) == [200, 200, 200, 201]
    assert len({r.json()["id"] for r in results}) == 1
    assert len(client.get("/conversions", headers=headers).json()) == 1


def test_migration_roundtrip(client):
    from alembic import command
    from alembic.config import Config
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import inspect

    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        command.downgrade(config, "base")
        assert set(inspect(connection).get_table_names()) == {"alembic_version"}
        command.upgrade(config, "head")
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        connection.execute(text("DROP TABLE alembic_version"))


def test_application_requires_postgres():
    from pydantic import ValidationError
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            database_url="sqlite:///ignored.db", environment="development", auto_create_tables=False
        )
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+psycopg://user:pass@localhost/db",
            environment="development",
            auto_create_tables=True,
        )


def sign_in(client, workspace_id, email, password="affiliate-password-123"):
    result = client.post("/auth/login", json={"email": email, "password": password})
    assert result.status_code == 200, result.text
    return {"Authorization": "Bearer " + result.json()["access_token"]}


def test_publishers_and_parent_isolation(client):
    admin_headers, admin = register(client)
    affiliate, offer, link, original_click = setup_offer(client, admin_headers)
    affiliate_headers = sign_in(client, admin["workspace_id"], affiliate["email"])
    publishers = []
    for name in ("one", "two"):
        response = client.post(
            "/users",
            headers=affiliate_headers,
            json={
                "email": f"{name}@example.com",
                "password": "publisher-password-123",
                "role": "publisher",
            },
        )
        assert response.status_code == 201, response.text
        publishers.append(response.json())
    assert (
        client.post(
            "/users",
            headers=affiliate_headers,
            json={
                "email": "bad@example.com",
                "password": "publisher-password-123",
                "role": "admin",
            },
        ).status_code
        == 403
    )
    links = []
    for publisher in publishers:
        response = client.post(
            "/links",
            headers=affiliate_headers,
            json={"offer_id": offer["id"], "affiliate_id": publisher["id"]},
        )
        assert response.status_code == 201, response.text
        links.append(response.json())
    client.get("/t/" + links[0]["id"], follow_redirects=False)
    first_headers = sign_in(
        client, admin["workspace_id"], publishers[0]["email"], "publisher-password-123"
    )
    assert len(client.get("/links", headers=first_headers).json()) == 1
    assert client.get("/reports/summary", headers=first_headers).json()["clicks"] == 1
    assert client.get("/users", headers=first_headers).status_code == 403
    assert client.get("/reports/summary", headers=affiliate_headers).json()["clicks"] == 2
    other = client.post(
        "/users",
        headers=admin_headers,
        json={
            "email": "other@example.com",
            "password": "affiliate-password-123",
            "role": "affiliate",
        },
    ).json()
    other_headers = sign_in(client, admin["workspace_id"], other["email"])
    assert (
        client.patch(
            "/users/" + publishers[0]["id"], headers=other_headers, json={"active": False}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/links",
            headers=other_headers,
            json={"offer_id": offer["id"], "affiliate_id": publishers[0]["id"]},
        ).status_code
        == 403
    )
    client.patch("/users/" + affiliate["id"], headers=admin_headers, json={"active": False})
    assert client.get("/me", headers=first_headers).status_code == 403
    assert client.get("/t/" + links[0]["id"], follow_redirects=False).status_code == 410


def test_refresh_rotation_and_replay_revocation(client):
    headers, admin = register(client)
    old_refresh = client.cookies.get("refresh_token")
    assert client.post("/auth/refresh").status_code == 403
    response = client.post(
        "/auth/refresh", headers={"X-CSRF-Token": client.cookies.get("csrf_token")}
    )
    assert response.status_code == 200, response.text
    new_headers = {"Authorization": "Bearer " + response.json()["access_token"]}
    assert client.get("/me", headers=new_headers).status_code == 200
    client.cookies.set("refresh_token", old_refresh, domain="testserver.local", path="/auth")
    assert (
        client.post(
            "/auth/refresh", headers={"X-CSRF-Token": client.cookies.get("csrf_token")}
        ).status_code
        == 401
    )
    assert client.get("/me", headers=new_headers).status_code == 401


def test_logout_invalidates_access_token(client):
    headers, admin = register(client)
    assert (
        client.post(
            "/auth/logout", headers={"X-CSRF-Token": client.cookies.get("csrf_token")}
        ).status_code
        == 204
    )
    assert client.get("/me", headers=headers).status_code == 401


def test_email_verification_is_required(client, monkeypatch):
    from app.config import get_settings
    from app.models import OutboxEvent
    from app.services import decrypt
    import json

    monkeypatch.setattr(get_settings(), "require_email_verification", True)
    result = client.post(
        "/auth/register",
        json={
            **signup_body("Verified network"),
            "email": "verify@example.com",
            "password": "verification-password-123",
        },
    )
    assert result.status_code == 201 and result.json()["verification_required"]
    body = {
        "email": "verify@example.com",
        "password": "verification-password-123",
    }
    assert client.post("/auth/login", json=body).status_code == 403
    with SessionLocal() as db:
        event = db.scalar(select(OutboxEvent))
        assert "verify@example.com" not in event.payload
        raw = json.loads(decrypt(event.payload))["url"].split("#token=")[1]
    assert client.post("/auth/verify", json={"token": raw}).status_code == 200
    assert client.post("/auth/verify", json={"token": raw}).status_code == 400
    assert client.post("/auth/login", json=body).status_code == 200


def test_password_reset_revokes_sessions_and_is_single_use(client):
    from app.auth import issue_action
    from app.models import OutboxEvent
    from app.services import decrypt
    import json

    headers, admin = register(client)
    with SessionLocal.begin() as db:
        issue_action(db, db.get(User, admin["id"]), "reset")
    with SessionLocal() as db:
        raw = json.loads(decrypt(db.scalar(select(OutboxEvent)).payload))["url"].split("#token=")[1]
    body = {"token": raw, "password": "changed-password-123"}
    assert client.post("/auth/reset-password", json=body).status_code == 200
    assert client.get("/me", headers=headers).status_code == 401
    assert client.post("/auth/reset-password", json=body).status_code == 400
    assert sign_in(client, admin["workspace_id"], admin["email"], "changed-password-123")


def test_trial_expires_and_billing_remains_accessible(client):
    from datetime import datetime, timedelta, timezone
    from app.models import Workspace

    headers, admin = register(client)
    with SessionLocal.begin() as db:
        workspace = db.get(Workspace, admin["workspace_id"])
        assert (
            14.9
            < (
                (
                    workspace.trial_ends_at.astimezone(timezone.utc)
                    if workspace.trial_ends_at.tzinfo
                    else workspace.trial_ends_at.replace(tzinfo=timezone.utc)
                )
                - datetime.now(timezone.utc)
            ).total_seconds()
            / 86400
            <= 15
        )
        workspace.trial_ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert client.get("/offers", headers=headers).status_code == 402
    assert client.get("/workspace", headers=headers).status_code == 200
    assert client.get("/billing/plans", headers=headers).status_code == 200


def test_advertiser_cannot_see_other_advertiser_or_margin(client):
    headers, admin = register(client)
    advertisers = []
    for index in (1, 2):
        advertisers.append(
            client.post(
                "/users",
                headers=headers,
                json={
                    "email": f"advertiser{index}@example.com",
                    "password": "advertiser-password-123",
                    "role": "advertiser",
                },
            ).json()
        )
    advertiser_headers = sign_in(
        client, admin["workspace_id"], advertisers[0]["email"], "advertiser-password-123"
    )
    offer = client.post(
        "/offers",
        headers=headers,
        json={
            "name": "Private offer",
            "destination_url": "https://example.com",
            "revenue": "100",
            "payout": "20",
            "advertiser_id": advertisers[1]["id"],
        },
    ).json()
    assert client.get("/offers", headers=advertiser_headers).json() == []
    assert (
        client.post(
            "/offers/" + offer["id"] + "/rotate-postback-key", headers=advertiser_headers
        ).status_code
        == 404
    )
    own = client.post(
        "/offers",
        headers=advertiser_headers,
        json={
            "name": "My offer",
            "destination_url": "https://example.com",
            "revenue": "100",
            "payout": "20",
        },
    )
    assert own.status_code == 201 and not own.json()["active"]
    rows = client.get("/offers", headers=advertiser_headers).json()
    assert len(rows) == 1 and "payout" not in rows[0]


def test_body_limits_security_headers_and_api_metadata(client):
    response = client.post(
        "/auth/login", content=b"a" * 70000, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.headers["x-request-id"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/").json()["service"] == "Affiliate Network API"
    assert client.get("/metrics").status_code == 401
    assert (
        client.get(
            "/metrics", headers={"Authorization": "Bearer " + os.environ["METRICS_TOKEN"]}
        ).status_code
        == 200
    )
    response = client.post("/auth/login", json={"password": "secret"})
    assert "secret" not in response.text


def test_worker_retries_and_recovers_expired_lease(client, monkeypatch):
    from app import worker
    from app.models import OutboxEvent, now
    from app.services import enqueue
    from datetime import timedelta

    headers, admin = register(client)
    with SessionLocal.begin() as db:
        event = enqueue(db, admin["workspace_id"], "email", {"to": "test@example.com"})
        db.flush()
        identifier = event.id

    def fail(*args):
        raise RuntimeError("secret must not leak")

    monkeypatch.setattr(worker, "dispatch", fail)
    assert worker.run_once()
    with SessionLocal.begin() as db:
        event = db.get(OutboxEvent, identifier)
        assert event.status == "pending" and event.last_error == "RuntimeError"
        event.status = "processing"
        event.lease_until = now() - timedelta(seconds=1)
    monkeypatch.setattr(worker, "dispatch", lambda *args: None)
    assert worker.run_once()
    with SessionLocal() as db:
        event = db.get(OutboxEvent, identifier)
        assert event.status == "delivered" and event.attempts == 2


def test_webhook_ssrf_rejected(monkeypatch):
    from app.delivery import validate_destination
    from app.config import get_settings
    from fastapi import HTTPException
    import socket

    monkeypatch.setattr(get_settings(), "outbound_allowed_hosts", ["evil.example"])
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(HTTPException):
        validate_destination("https://evil.example")
    with pytest.raises(HTTPException):
        validate_destination("http://evil.example")


def test_signed_billing_webhooks_are_deduplicated(client, monkeypatch):
    import json
    import hmac
    import hashlib
    from app.config import get_settings
    from app.models import Workspace, OutboxEvent

    headers, admin = register(client)
    monkeypatch.setattr(get_settings(), "razorpay_webhook_secret", "test-webhook-secret")
    with SessionLocal.begin() as db:
        db.get(Workspace, admin["workspace_id"]).razorpay_subscription_id = "sub_test"
    raw = json.dumps({"payload": {"subscription": {"entity": {"id": "sub_test"}}}}).encode()
    signature = hmac.new(b"test-webhook-secret", raw, hashlib.sha256).hexdigest()
    request_headers = {
        "X-Razorpay-Event-Id": "event-1",
        "X-Razorpay-Signature": signature,
        "Content-Type": "application/json",
    }
    assert (
        client.post(
            "/billing/webhook",
            content=raw,
            headers={**request_headers, "X-Razorpay-Signature": "wrong"},
        ).status_code
        == 401
    )
    assert client.post("/billing/webhook", content=raw, headers=request_headers).status_code == 200
    assert client.post("/billing/webhook", content=raw, headers=request_headers).json()["duplicate"]
    with SessionLocal() as db:
        assert len(list(db.scalars(select(OutboxEvent).where(OutboxEvent.kind == "billing")))) == 1


def test_platform_role_is_not_assignable_from_api(client):
    headers, admin = register(client)
    assert client.get("/platform/workspaces", headers=headers).status_code == 403
    assert (
        client.post(
            "/users",
            headers=headers,
            json={
                "email": "owner@example.com",
                "password": "owner-password-123",
                "role": "platform_admin",
            },
        ).status_code
        == 422
    )


def test_signup_options_match_requested_business_types(client):
    options = client.get("/auth/registration-options").json()
    assert [row["label"] for row in options["business_type"]] == [
        "Brand/Direct Advertiser",
        "Agency",
        "Affiliate/Ad Network",
        "Mobile Apps",
        "Media Buyer",
        "Social Media Advertising",
        "Influencer / Creator",
    ]
    assert len(options["countries"]) > 200
    assert options["trial_days"] == 15 and not options["credit_card_required"]
    assert not options["setup_fee"]


def test_signup_contact_fields_and_validation(client):
    body = signup_body()
    body.update(
        name="Jane Doe",
        teams_enabled=True,
        teams_email="jane.teams@example.com",
        mobile="00919876543210",
        usage_types=["lead_generation", "e_commerce"],
        business_type="social_media_advertising",
    )
    response = client.post("/auth/register", json=body)
    assert response.status_code == 201, response.text
    headers = {"Authorization": "Bearer " + response.json()["access_token"]}
    assert client.get("/me", headers=headers).json()["name"] == "Jane Doe"
    profile = client.get("/workspace", headers=headers).json()["profile"]
    assert (
        profile["mobile"] == "+919876543210" and profile["teams_email"] == "jane.teams@example.com"
    )
    for changes in (
        {"terms_accepted": False},
        {"teams_email": None},
        {"country": "ZZ"},
        {"mobile": "123"},
        {"business_type": "unsupported"},
        {"usage_types": []},
    ):
        assert client.post("/auth/register", json={**body, **changes}).status_code == 422


def test_commission_reversals_and_retired_payouts(client):
    from app.models import CommissionEntry

    headers, admin = register(client)
    affiliate, offer, link, click_id = setup_offer(client, headers)
    conversion = client.post(
        "/postbacks/" + offer["id"],
        headers={"X-Postback-Key": offer["postback_key"]},
        json={"click_id": click_id, "transaction_id": "ledger-order"},
    ).json()
    endpoint = "/conversions/" + conversion["id"]
    assert client.patch(endpoint, headers=headers, json={"status": "approved"}).status_code == 200
    assert client.patch(endpoint, headers=headers, json={"status": "approved"}).status_code == 200
    assert client.get("/payouts/balances", headers=headers).json()[0]["balance"] == "7.2500"
    body = {
        "affiliate_id": affiliate["id"],
        "amount": "7.2500",
        "currency": "USD",
        "reference": "bank-001",
    }
    assert client.post("/payouts", headers=headers, json=body).status_code == 410
    assert (
        client.post("/payouts", headers=headers, json={**body, "reference": "bank-002"}).status_code
        == 410
    )
    assert client.patch(endpoint, headers=headers, json={"status": "rejected"}).status_code == 200
    assert client.get("/payouts/balances", headers=headers).json()[0]["balance"] == "0.0000"
    with SessionLocal() as db:
        assert len(list(db.scalars(select(CommissionEntry)))) == 2


def test_redis_outage_fails_closed_without_breaking_liveness(client, monkeypatch):
    from app.middleware import SecurityMiddleware
    from redis.exceptions import RedisError

    async def unavailable(*args):
        raise RedisError("sensitive connection details")

    monkeypatch.setattr(SecurityMiddleware, "allowed", unavailable)
    response = client.get("/me")
    assert response.status_code == 503 and "sensitive" not in response.text
    assert client.get("/health").status_code == 200


def test_unhandled_errors_are_redacted(client, monkeypatch, caplog):
    from app import routes

    headers, admin = register(client)

    def broken(*args):
        raise RuntimeError("password=super-secret")

    monkeypatch.setattr(routes, "output_user", broken)
    response = client.get("/me", headers=headers)
    assert response.status_code == 500
    assert response.headers["x-request-id"]
    assert "super-secret" not in response.text and "super-secret" not in caplog.text


def test_separate_frontend_csrf_bootstrap_and_origin_check(client):
    headers, admin = register(client)
    result = client.get("/auth/csrf", headers={"Origin": "http://localhost:3000"})
    assert result.status_code == 200 and result.json()["csrf_token"] == client.cookies.get(
        "csrf_token"
    )
    assert (
        client.get("/auth/csrf", headers={"Origin": "https://attacker.example"}).status_code == 403
    )
    assert (
        client.post(
            "/auth/refresh",
            headers={
                "Origin": "https://attacker.example",
                "X-CSRF-Token": client.cookies.get("csrf_token"),
            },
        ).status_code
        == 403
    )


def test_signup_rejects_stale_policy_version(client):
    assert (
        client.post("/auth/register", json={**signup_body(), "legal_version": "old"}).status_code
        == 422
    )


@pytest.mark.skipif(
    not os.environ.get("REDIS_TEST_URL"), reason="A dedicated Redis test service is required"
)
def test_redis_limits_are_shared_between_instances():
    import asyncio
    import uuid
    import hashlib
    from app.middleware import SecurityMiddleware
    from redis.asyncio import Redis

    async def exercise():
        identity = uuid.uuid4().hex
        first = SecurityMiddleware(None)
        second = SecurityMiddleware(None)
        first.redis = Redis.from_url(os.environ["REDIS_TEST_URL"])
        second.redis = Redis.from_url(os.environ["REDIS_TEST_URL"])
        try:
            assert await first.allowed(identity, "integration", 1)
            assert not await second.allowed(identity, "integration", 1)
        finally:
            await first.redis.delete(
                "rate:integration:" + hashlib.sha256(identity.encode()).hexdigest()
            )
            await first.redis.aclose()
            await second.redis.aclose()

    asyncio.run(exercise())


def test_email_password_login_without_workspace_or_mfa(client):
    headers, admin = register(client)
    response = client.post(
        "/auth/login", json={"email": admin["email"], "password": "a-strong-password-123"}
    )
    assert response.status_code == 200, response.text
    assert "workspace_id" not in response.json()["user"]
    schema = client.get("/openapi.json").json()["components"]["schemas"]["Login"]["properties"]
    assert set(schema) == {"email", "password"}
    assert client.post("/auth/mfa/setup", headers=headers, json={}).status_code == 404


def test_duplicate_email_is_rejected_across_workspaces(client):
    from app.models import Workspace

    headers, admin = register(client)
    response = client.post(
        "/auth/register", json={**signup_body("Other Agency"), "email": admin["email"]}
    )
    assert response.status_code == 409
    with SessionLocal() as db:
        assert len(list(db.scalars(select(Workspace)))) == 1


def test_targeting_schedule_and_untrusted_geo_headers(client, monkeypatch):
    from app import tracking

    headers, admin = register(client)
    affiliate, offer, link, click_id = setup_offer(client, headers)
    endpoint = "/offers/" + offer["id"] + "/rules"
    assert (
        client.put(
            endpoint,
            headers=headers,
            json={
                "countries": ["IN"],
                "devices": ["mobile"],
                "operating_systems": ["android"],
                "languages": ["en"],
            },
        ).status_code
        == 200
    )
    ua = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14) Mobile",
        "Accept-Language": "en-IN",
        "X-Country": "IN",
    }
    assert client.get("/t/" + link["id"], headers=ua, follow_redirects=False).status_code == 403
    original = tracking.context

    def located(request):
        metadata, address, agent = original(request)
        metadata["country"] = "IN"
        return metadata, address, agent

    monkeypatch.setattr(tracking, "context", located)
    assert client.get("/t/" + link["id"], headers=ua, follow_redirects=False).status_code == 302
    assert (
        client.get(
            "/t/" + link["id"], headers={**ua, "Accept-Language": "fr"}, follow_redirects=False
        ).status_code
        == 403
    )
    assert client.put(endpoint, headers=headers, json={"countries": ["ZZ"]}).status_code == 422


def test_concurrent_click_cap_cannot_be_exceeded(client):
    from concurrent.futures import ThreadPoolExecutor
    from app.models import OfferCounter

    headers, admin = register(client)
    affiliate, offer, link, click_id = setup_offer(client, headers)
    assert (
        client.put(
            "/offers/" + offer["id"] + "/rules",
            headers=headers,
            json={"daily_clicks": 3, "total_clicks": 3},
        ).status_code
        == 200
    )
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(
            pool.map(lambda _: client.get("/t/" + link["id"], follow_redirects=False), range(6))
        )
    assert [r.status_code for r in responses].count(302) == 2
    assert [r.status_code for r in responses].count(409) == 4
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(OfferCounter).where(
                    OfferCounter.offer_id == offer["id"], OfferCounter.period == "total"
                )
            ).clicks
            == 3
        )


def test_conversion_budget_and_duplicate_retry(client):
    headers, admin = register(client)
    affiliate, offer, link, click_id = setup_offer(client, headers)
    assert (
        client.put(
            "/offers/" + offer["id"] + "/rules",
            headers=headers,
            json={"total_conversions": 1, "daily_revenue": "10.5000"},
        ).status_code
        == 200
    )
    endpoint = "/postbacks/" + offer["id"]
    body = {"click_id": click_id, "transaction_id": "budget-1"}
    key = {"X-Postback-Key": offer["postback_key"]}
    assert client.post(endpoint, headers=key, json=body).status_code == 201
    assert client.post(endpoint, headers=key, json=body).status_code == 200
    response = client.get("/t/" + link["id"], follow_redirects=False)
    next_click = __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(
        urlsplit(response.headers["location"]).query
    )["click_id"][0]
    assert (
        client.post(
            endpoint, headers=key, json={"click_id": next_click, "transaction_id": "budget-2"}
        ).status_code
        == 409
    )


def test_fraud_bot_and_conversion_timing(client):
    from app.models import Click
    from datetime import datetime, timedelta, timezone

    headers, admin = register(client)
    affiliate, offer, link, click_id = setup_offer(client, headers)
    assert (
        client.get(
            "/t/" + link["id"], headers={"User-Agent": "Googlebot/2.1"}, follow_redirects=False
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/offers/" + offer["id"] + "/rules",
            headers=headers,
            json={"minimum_conversion_seconds": 30},
        ).status_code
        == 200
    )
    body = {"click_id": click_id, "transaction_id": "speed-1"}
    key = {"X-Postback-Key": offer["postback_key"]}
    assert client.post("/postbacks/" + offer["id"], headers=key, json=body).status_code == 403
    with SessionLocal.begin() as db:
        db.get(Click, click_id).created_at = datetime.now(timezone.utc) - timedelta(seconds=40)
    assert client.post("/postbacks/" + offer["id"], headers=key, json=body).status_code == 201
    events = client.get("/fraud/events", headers=headers).json()
    assert {e["reason"] for e in events} == {"bot_user_agent", "conversion_too_fast"}
    assert client.post("/fraud/events/" + events[0]["id"] + "/review", headers=headers).json()[
        "reviewed"
    ]


def test_custom_domain_verification_and_tenant_bound_host(client, monkeypatch):
    from app import domains
    from app.config import get_settings

    headers, admin = register(client)
    affiliate, offer, link, click_id = setup_offer(client, headers)
    monkeypatch.setattr(get_settings(), "tracking_cname", "tracking.example.com")
    result = client.post(
        "/tracking-domains", headers=headers, json={"hostname": "click.agency.example"}
    )
    assert result.status_code == 201, result.text
    domain = result.json()
    assert (
        client.get(
            "/t/" + link["id"], headers={"Host": domain["hostname"]}, follow_redirects=False
        ).status_code
        == 400
    )
    monkeypatch.setattr(domains, "verify_dns", lambda record: True)
    assert (
        client.post("/tracking-domains/" + domain["id"] + "/verify", headers=headers).status_code
        == 200
    )
    assert (
        client.get(
            "/t/" + link["id"], headers={"Host": domain["hostname"]}, follow_redirects=False
        ).status_code
        == 302
    )
    assert client.get("/me", headers={**headers, "Host": domain["hostname"]}).status_code == 400
    urls = client.get("/links?tracking_domain_id=" + domain["id"], headers=headers).json()
    assert urls[0]["url"].startswith("https://click.agency.example/")
    other_headers, other = register(client, "Another Network")
    _, _, other_link, _ = setup_offer_for_other_network(client, other_headers)
    assert (
        client.get(
            "/t/" + other_link["id"], headers={"Host": domain["hostname"]}, follow_redirects=False
        ).status_code
        == 404
    )
    assert client.delete("/tracking-domains/" + domain["id"], headers=headers).status_code == 200


def setup_offer_for_other_network(client, headers):
    affiliate = client.post(
        "/users",
        headers=headers,
        json={
            "email": "other-network-affiliate@example.com",
            "password": "affiliate-password-123",
            "role": "affiliate",
        },
    ).json()
    offer = client.post(
        "/offers",
        headers=headers,
        json={
            "name": "Other offer",
            "destination_url": "https://example.com",
            "revenue": "10.5",
            "payout": "7.25",
        },
    ).json()
    link = client.post(
        "/links", headers=headers, json={"offer_id": offer["id"], "affiliate_id": affiliate["id"]}
    ).json()
    return affiliate, offer, link, None


def test_detailed_reports_exports_and_role_visibility(client):
    headers, admin = register(client)
    affiliate, offer, link, click_id = setup_offer(client, headers)
    conversion = client.post(
        "/postbacks/" + offer["id"],
        headers={"X-Postback-Key": offer["postback_key"]},
        json={"click_id": click_id, "transaction_id": "=DANGEROUS()"},
    ).json()
    client.patch("/conversions/" + conversion["id"], headers=headers, json={"status": "approved"})
    assert len(client.get("/reports/clicks", headers=headers).json()) == 1
    groups = client.get("/reports/grouped?group_by=offer", headers=headers)
    assert groups.status_code == 200, groups.text
    assert groups.json()[0]["approved_conversions"] == 1
    exported = client.get("/reports/export/conversions", headers=headers)
    assert exported.status_code == 200 and "'=DANGEROUS()" in exported.text
    assert exported.headers["x-export-truncated"] == "false"
    affiliate_headers = sign_in(client, admin["workspace_id"], affiliate["email"])
    rows = client.get("/reports/conversions", headers=affiliate_headers).json()
    assert "revenue" not in rows[0]
    assert (
        "revenue"
        not in client.get(
            "/reports/export/conversions", headers=affiliate_headers
        ).text.splitlines()[0]
    )
    other_headers, other = register(client, "Report Other")
    assert client.get("/reports/clicks", headers=other_headers).json() == []
    assert (
        client.get(
            "/reports/clicks?start=2020-01-01T00:00:00Z&end=2026-01-01T00:00:00Z", headers=headers
        ).status_code
        == 422
    )


def test_billing_activation_renewal_grace_halt_resume_cancel(client, monkeypatch):
    from app import billing
    from app.config import get_settings
    from app.models import Workspace
    from datetime import datetime, timedelta, timezone

    headers, admin = register(client)
    monkeypatch.setattr(get_settings(), "razorpay_plans", {"starter": "plan_starter"})
    with SessionLocal.begin() as db:
        db.get(Workspace, admin["workspace_id"]).razorpay_subscription_id = "sub_lifecycle"
    state = {
        "id": "sub_lifecycle",
        "plan_id": "plan_starter",
        "status": "active",
        "current_end": int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp()),
    }
    monkeypatch.setattr(billing, "provider_request", lambda *args, **kwargs: state.copy())
    assert client.post("/billing/reconcile", headers=headers).json()["status"] == "active"
    state.update(current_end=int((datetime.now(timezone.utc) + timedelta(days=60)).timestamp()))
    renewed = client.post("/billing/reconcile", headers=headers).json()["paid_until"]
    assert renewed
    with SessionLocal.begin() as db:
        db.get(Workspace, admin["workspace_id"]).paid_until = datetime.now(
            timezone.utc
        ) - timedelta(hours=1)
    state["status"] = "pending"
    assert client.post("/billing/reconcile", headers=headers).json()["grace_until"]
    assert client.get("/offers", headers=headers).status_code == 200
    state["status"] = "halted"
    client.post("/billing/reconcile", headers=headers)
    assert client.get("/offers", headers=headers).status_code == 402
    state["status"] = "active"
    assert client.post("/billing/resume", headers=headers).json()["status"] == "active"
    state["status"] = "paused"
    assert client.post("/billing/pause", headers=headers).json()["status"] == "paused"
    state["status"] = "cancelled"
    client.post("/billing/reconcile", headers=headers)
    assert client.get("/offers", headers=headers).status_code == 402


def test_subscription_checkout_uses_fifteen_day_trial_and_existing_checkout(client, monkeypatch):
    from app import billing
    from app.config import get_settings
    from datetime import datetime, timezone

    headers, admin = register(client)
    monkeypatch.setattr(get_settings(), "razorpay_plans", {"starter": "plan_starter"})
    calls = []

    def provider(method, path, body=None):
        calls.append((method, path, body))
        return {
            "id": "sub_checkout",
            "status": "created",
            "short_url": "https://rzp.io/test-checkout",
        }

    monkeypatch.setattr(billing, "provider_request", provider)
    assert (
        client.post("/billing/subscribe", headers=headers, json={"plan": "starter"}).status_code
        == 200
    )
    start = calls[0][2]["start_at"]
    assert 14.9 < (start - datetime.now(timezone.utc).timestamp()) / 86400 <= 15
    assert (
        client.post("/billing/subscribe", headers=headers, json={"plan": "starter"}).status_code
        == 200
    )
    assert [call[0] for call in calls] == ["POST", "GET"]


def test_invoice_provider_query_is_scoped_to_subscription(client, monkeypatch):
    from app import billing
    from app.db import SessionLocal
    from app.models import Workspace

    headers, account = register(client)
    with SessionLocal() as db:
        workspace = db.get(Workspace, account["workspace_id"])
        workspace.razorpay_subscription_id = "sub_invoicecheck"
        db.commit()
    calls = []

    def provider(method, path, body=None):
        calls.append((method, path))
        return {"items": [{"id": "inv_example"}]}

    monkeypatch.setattr(billing, "provider_request", provider)
    result = client.get("/billing/invoices", headers=headers)
    assert result.status_code == 200
    assert calls == [("GET", "invoices?subscription_id=sub_invoicecheck")]


def test_domain_certificate_gate_and_failed_dns_verification(client, monkeypatch):
    from app.config import get_settings
    from app import domains

    headers, admin = register(client)
    monkeypatch.setattr(get_settings(), "tracking_cname", "tracking.example.com")
    monkeypatch.setattr(get_settings(), "domain_certificate_token", "certificate-test-secret")
    domain = client.post(
        "/tracking-domains", headers=headers, json={"hostname": "pending.agency.example"}
    ).json()
    monkeypatch.setattr(domains, "verify_dns", lambda record: False)
    assert (
        client.post("/tracking-domains/" + domain["id"] + "/verify", headers=headers).status_code
        == 422
    )
    assert (
        client.get(
            "/internal/certificates/allow?domain=pending.agency.example&token=certificate-test-secret"
        ).status_code
        == 403
    )
    assert (
        client.get("/internal/certificates/allow?domain=localhost&token=wrong").status_code == 403
    )
