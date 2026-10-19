"""Only platform owners may change network roles; old credentials lose access."""

import pytest
from test_api import client as api_client, register, sign_in  # noqa: F401
from test_campaign_invoices import partner
from app.db import SessionLocal
from app.models import User, AuditEvent
from sqlalchemy import select


@pytest.fixture
def client(api_client):  # noqa: F811
    return api_client


def owner(client):
    headers, account = register(client, "Platform operations")
    with SessionLocal.begin() as db:
        db.get(User, account["id"]).role = "platform_admin"
    return headers


def test_super_admin_can_promote_advertiser_and_revoke_old_session(client):
    root = owner(client)
    admin, network_admin = register(client, "Customer network")
    advertiser, old_session = partner(client, admin, "advertiser", "customer-advertiser")
    network = network_admin["workspace_id"]
    path = f"/platform/networks/{network}/users/{advertiser['id']}/access"
    body = {
        "role": "admin",
        "active": True,
        "reason": "Customer requested network administrator access",
    }
    assert client.patch(path, headers=admin, json=body).status_code == 403
    assert client.patch(path, headers=old_session, json=body).status_code == 403
    response = client.patch(path, headers=root, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "admin" and response.json()["sessions_revoked"]
    assert client.get("/me", headers=old_session).status_code == 401
    fresh = sign_in(client, None, advertiser["email"])
    assert client.get("/me", headers=fresh).json()["role"] == "admin"
    assert client.get("/users", headers=fresh).status_code == 200
    assert client.get("/platform/summary", headers=fresh).status_code == 403
    assert (
        client.patch(path, headers=root, json={**body, "role": "platform_admin"}).status_code == 422
    )
    with SessionLocal() as db:
        entry = db.scalar(select(AuditEvent).where(AuditEvent.action == "platform.user_access"))
        assert entry.workspace_id == network and "advertiser->admin" in entry.detail


def test_super_admin_protects_last_admin_and_network_boundaries(client):
    root = owner(client)
    admin, account = register(client, "One admin network")
    other_admin, other = register(client, "Other network")
    body = {"role": "advertiser", "active": True, "reason": "Change account access for testing"}
    path = f"/platform/networks/{account['workspace_id']}/users/"
    assert (
        client.patch(path + account["id"] + "/access", headers=root, json=body).status_code == 409
    )
    assert client.patch(path + other["id"] + "/access", headers=root, json=body).status_code == 404
    assert (
        client.get(f"/platform/networks/{account['workspace_id']}/users", headers=admin).status_code
        == 403
    )
    response = client.get(f"/platform/networks/{account['workspace_id']}/users", headers=root)
    assert response.status_code == 200 and len(response.json()) == 1
    assert "password_hash" not in response.text
    for dataset in ["overview", "campaigns", "conversions", "invoices", "domains", "audit"]:
        assert (
            client.get(
                f"/platform/networks/{account['workspace_id']}/{dataset}", headers=root
            ).status_code
            == 200
        )


def test_operator_bootstrap_uses_existing_verified_account_and_revokes_sessions(client):
    from scripts.grant_owner import grant_owner

    admin, account = register(client, "Owner bootstrap network")
    member, session = partner(client, admin, "advertiser", "bootstrap-owner")
    grant_owner(member["email"])
    assert client.get("/me", headers=session).status_code == 401
    fresh = sign_in(client, None, member["email"])
    assert client.get("/platform/summary", headers=fresh).status_code == 200
    with pytest.raises(ValueError):
        grant_owner("not-registered@example.com")


def test_initial_owner_setup_does_not_revoke_owner_on_restart(client):
    from scripts.grant_owner import grant_owner

    admin, account = register(client, "Initial owner network")
    member, old = partner(client, admin, "advertiser", "initial-owner")
    grant_owner(member["email"], initial_only=True)
    fresh = sign_in(client, None, member["email"])
    grant_owner(member["email"], initial_only=True)
    assert client.get("/platform/summary", headers=fresh).status_code == 200
    with pytest.raises(ValueError):
        grant_owner("another@example.com", initial_only=True)
