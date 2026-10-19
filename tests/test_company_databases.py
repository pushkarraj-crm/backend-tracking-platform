"""Real PostgreSQL isolation tests; generated databases are disposable test fixtures."""

from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import NullPool
from sqlalchemy.engine import make_url
import secrets
import uuid
import json
import hmac
import hashlib

from test_api import client as company_client, signup_body  # noqa: F401 -- shared fixture
from app.config import get_settings
from app.db import ControlSession, engine
from app.main import app
from app.models import Workspace, User, OutboxEvent
from app.security import passwords
from app.tenancy import Company, company_slug, company_sessions, for_each_company, company_context


@pytest.mark.parametrize(
    "name,slug",
    [
        ("Media Kendra", "media"),
        (" Media House ", "media"),
        ("API Ltd", "company-api"),
        ("*** Ltd", "company"),
        ("Café Network", "cafe"),
    ],
)
def test_slug(name, slug):
    assert company_slug(name) == slug


def test_real_company_databases(company_client, monkeypatch):  # noqa: F811 -- pytest fixture
    client = company_client
    if engine.dialect.name != "postgresql":
        pytest.skip("Company database isolation requires PostgreSQL with CREATEDB/CREATEROLE")
    settings = get_settings()
    monkeypatch.setattr(settings, "company_databases", True)
    monkeypatch.setattr(settings, "company_base_domain", "localhost")
    monkeypatch.setattr(settings, "company_frontend_port", 3000)
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "auth_rate_per_minute", 100)
    admin = create_engine(
        settings.company_database_admin_url or settings.database_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    provisioner = "provisioner_" + uuid.uuid4().hex
    provisioner_password = secrets.token_urlsafe(32)
    with admin.connect() as connection:
        connection.exec_driver_sql(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER CREATEDB CREATEROLE NOINHERIT")
            .format(sql.Identifier(provisioner), sql.Literal(provisioner_password))
            .as_string()
        )
    monkeypatch.setattr(
        settings,
        "company_database_admin_url",
        make_url(settings.company_database_admin_url or settings.database_url)
        .set(username=provisioner, password=provisioner_password)
        .render_as_string(hide_password=False),
    )
    try:
        companies = []
        for name in ["Media Kendra", "Media House"]:
            response = client.post("/auth/register", json=signup_body(name))
            assert response.status_code == 201, response.text
            companies.append(response.json())
        assert [c["company_slug"] for c in companies] == ["media", "media-2"]
        assert companies[0]["dashboard_url"] == "http://media.localhost:3000"
        assert "access_token" not in companies[0]
        assert client.get("/offers").status_code == 404
        assert client.get("/me", headers={"X-Company-Slug": "unknown"}).status_code == 404
        assert client.get("/me", headers={"X-Company-Slug": "media.evil"}).status_code == 400
        with (
            TestClient(app, base_url=companies[0]["dashboard_url"]) as a,
            TestClient(app, base_url=companies[1]["dashboard_url"]) as b,
        ):
            tokens = []
            for browser, name in [(a, "Media Kendra"), (b, "Media House")]:
                body = signup_body(name)
                response = browser.post(
                    "/auth/login", json={k: body[k] for k in ["email", "password"]}
                )
                assert response.status_code == 200, response.text
                tokens.append(response.json())
            ha = {"Authorization": "Bearer " + tokens[0]["access_token"]}
            hb = {"Authorization": "Bearer " + tokens[1]["access_token"]}
            assert b.get("/me", headers=ha).status_code == 401
            assert a.get("/me", headers=hb).status_code == 401
            assert a.get("/me", headers={**ha, "X-Company-Slug": "media-2"}).status_code == 400
            affiliate = a.post(
                "/users",
                headers=ha,
                json={
                    "email": "affiliate@example.com",
                    "password": "safe-affiliate-password-123",
                    "role": "affiliate",
                },
            ).json()
            publisher = a.post(
                "/users",
                headers=ha,
                json={
                    "email": "publisher@example.com",
                    "password": "safe-publisher-password-123",
                    "role": "publisher",
                    "parent_user_id": affiliate["id"],
                },
            )
            assert publisher.status_code == 201, publisher.text
            offer = a.post(
                "/offers",
                headers=ha,
                json={
                    "name": "Private A",
                    "destination_url": "https://example.com",
                    "revenue": "10",
                    "payout": "5",
                },
            ).json()
            assert "/c/media/postbacks/" in offer["postback_url"]
            assert b.get("/offers", headers=hb).json() == []
            link = a.post(
                "/links",
                headers=ha,
                json={"offer_id": offer["id"], "affiliate_id": affiliate["id"]},
            ).json()
            assert "/c/media/t/" in link["url"]
            click = client.get(urlsplit(link["url"]).path, follow_redirects=False)
            assert click.status_code == 302, click.text
            click_id = parse_qs(urlsplit(click.headers["location"]).query)["click_id"][0]
            postback = client.post(
                urlsplit(offer["postback_url"]).path,
                headers={"X-Postback-Key": offer["postback_key"]},
                json={"click_id": click_id, "transaction_id": "qa-one"},
            )
            assert postback.status_code == 201, postback.text
            assert (
                a.post(
                    "/auth/refresh",
                    headers={
                        "X-CSRF-Token": tokens[0]["csrf_token"],
                        "Origin": companies[1]["dashboard_url"],
                    },
                ).status_code
                == 403
            )
            assert (
                a.post(
                    "/auth/refresh",
                    headers={
                        "X-CSRF-Token": tokens[0]["csrf_token"],
                        "Origin": companies[0]["dashboard_url"],
                    },
                ).status_code
                == 200
            )
        visited = []

        def visit():
            with company_sessions(company_context.get())() as db:
                visited.append(db.scalar(select(Workspace.name)))

        for_each_company(visit)
        assert sorted(visited) == ["Media House", "Media Kendra"]
        with ControlSession() as db:
            records = list(db.scalars(select(Company)))
            assert len({r.database_name for r in records}) == 2
            assert all(r.status == "active" for r in records)
            assert db.scalar(select(Workspace.id)) is None
            with company_sessions(records[0])() as tenant_db:
                assert (
                    tenant_db.scalar(text("SELECT current_database()")) == records[0].database_name
                )
                assert not tenant_db.scalar(
                    text(
                        "SELECT rolcreatedb OR rolcreaterole OR rolsuper FROM pg_roles WHERE rolname=current_user"
                    )
                )
                other_connect = tenant_db.scalar(
                    text("SELECT has_database_privilege(current_user, :other, 'CONNECT')"),
                    {"other": records[1].database_name},
                )
                assert not other_connect, tenant_db.execute(
                    text(
                        "SELECT current_user, datname, datacl::text FROM pg_database WHERE datname LIKE 'company_%' ORDER BY datname"
                    )
                ).all()
        company = next(r for r in records if r.slug == "media")
        with company_sessions(company).begin() as db:
            db.get(Workspace, company.workspace_id).razorpay_subscription_id = "sub_company_qa"
        monkeypatch.setattr(settings, "razorpay_webhook_secret", "company-test-webhook-secret")
        raw = json.dumps(
            {
                "payload": {
                    "subscription": {
                        "entity": {"id": "sub_company_qa", "notes": {"company_slug": "media"}}
                    }
                }
            }
        ).encode()
        signature = hmac.new(
            settings.razorpay_webhook_secret.encode(), raw, hashlib.sha256
        ).hexdigest()
        delivered = client.post(
            "/billing/webhook",
            content=raw,
            headers={"X-Razorpay-Signature": signature, "X-Razorpay-Event-Id": "evt_company_qa"},
        )
        assert delivered.status_code == 200, delivered.text
        with company_sessions(company)() as db:
            assert db.scalar(select(OutboxEvent).where(OutboxEvent.kind == "billing")) is not None
        with ControlSession.begin() as db:
            owner_workspace = Workspace(name="Control plane")
            db.add(owner_workspace)
            db.flush()
            db.add(
                User(
                    workspace_id=owner_workspace.id,
                    email="owner@example.com",
                    password_hash=passwords.hash("safe-platform-password-123"),
                    role="platform_admin",
                    email_verified=True,
                )
            )
        owner_login = client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": "safe-platform-password-123"},
        )
        assert owner_login.status_code == 200, owner_login.text
        owner_headers = {"Authorization": "Bearer " + owner_login.json()["access_token"]}
        summary = client.get("/platform/summary", headers=owner_headers)
        assert summary.status_code == 200, summary.text
        assert summary.json()["workspaces"] == 2
        assert summary.json()["users"] == 4
        listed = client.get("/platform/workspaces", headers=owner_headers)
        assert {w["company_slug"] for w in listed.json()} == {"media", "media-2"}
        target_network = next(row["id"] for row in listed.json() if row["company_slug"] == "media")
        accounts = client.get(f"/platform/networks/{target_network}/users", headers=owner_headers)
        assert accounts.status_code == 200, accounts.text
        member = next(row for row in accounts.json() if row["role"] == "publisher")
        changed = client.patch(
            f"/platform/networks/{target_network}/users/{member['id']}/access",
            headers=owner_headers,
            json={
                "role": "staff",
                "active": True,
                "staff_permissions": ["reports:read"],
                "reason": "Customer requested reporting staff access",
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["role"] == "staff"
        suspended = client.patch(
            "/platform/workspaces/" + company.workspace_id,
            headers=owner_headers,
            json={"active": False},
        )
        assert suspended.status_code == 200, suspended.text
        with TestClient(app, base_url=companies[0]["dashboard_url"]) as a:
            body = signup_body("Media Kendra")
            assert (
                a.post("/auth/login", json={k: body[k] for k in ["email", "password"]}).status_code
                == 403
            )
    finally:
        # Only names allocated by this isolated test schema are eligible for cleanup.
        with ControlSession() as db:
            records = list(db.scalars(select(Company)))
        for record in records:
            import re

            assert re.fullmatch(r"company_[a-f0-9]{32}", record.database_name)
            with admin.connect() as connection:
                connection.exec_driver_sql(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                    .format(sql.Identifier(record.database_name))
                    .as_string()
                )
                connection.exec_driver_sql(
                    sql.SQL("DROP ROLE IF EXISTS {}")
                    .format(sql.Identifier(record.database_name))
                    .as_string()
                )
        with admin.connect() as connection:
            connection.exec_driver_sql(
                sql.SQL("DROP ROLE {}").format(sql.Identifier(provisioner)).as_string()
            )
        admin.dispose()
