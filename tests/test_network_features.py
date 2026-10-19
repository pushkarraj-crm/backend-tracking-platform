"""Network extensions: financial idempotency, isolation and safe configuration."""

from urllib.parse import parse_qs, urlsplit
import pytest
from test_api import client as api_client, register, sign_in  # noqa: F401
from test_campaign_invoices import campaign_flow, partner


@pytest.fixture
def client(api_client):  # noqa: F811
    return api_client


def goal(client, adv, campaign, **overrides):
    response = client.post(
        f"/campaigns/{campaign['id']}/events",
        headers=adv,
        json={
            "code": "qualified",
            "name": "Qualified lead",
            "payout": "7",
            "revenue": "9",
            **overrides,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def postback(client, campaign, click, transaction="order1", **extra):
    return client.post(
        "/postbacks/" + campaign["id"],
        headers={"X-Postback-Key": campaign["postback_key"]},
        json={"click_id": click, "transaction_id": transaction, **extra},
    )


def test_event_idempotency_and_repeat_sale_pricing(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    goal(client, adv, campaign, auto_approve=True)
    first = postback(client, campaign, click)
    second = postback(client, campaign, click, event_key="qualified")
    assert first.status_code == second.status_code == 201
    assert second.json()["status"] == "approved"
    replay = postback(client, campaign, click, event_key="qualified")
    assert replay.status_code == 200 and replay.json()["id"] == second.json()["id"]
    assert postback(client, campaign, click, "other", event_key="qualified").status_code == 409
    goal(client, adv, campaign, code="purchase", model="CPS", sale_percentage="10", repeatable=True)
    for transaction in ["sale1", "sale2"]:
        result = postback(
            client,
            campaign,
            click,
            transaction,
            event_key="purchase",
            event_type="sale",
            sale_amount="100",
            sale_currency="USD",
        )
        assert result.status_code == 201, result.text
    result = client.get("/reports/conversions", headers=pub)
    assert result.status_code == 200, result.text
    assert len(result.json()) == 4, result.json()
    eligible = client.get("/publisher-invoices/eligible-conversions", headers=pub).json()
    assert len(eligible) == 1 and eligible[0]["amount"] == "7.0000"


def test_commission_rule_uses_stored_source_not_postback_override(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    path = f"/campaigns/{campaign['id']}/commission-rules"
    body = {
        "name": "Search traffic",
        "publisher_id": publisher["id"],
        "payout": "8",
        "revenue": "10",
        "conditions": {"all": [{"field": "source", "operator": "eq", "value": "search"}]},
    }
    assert client.post(path, headers=adv, json=body).status_code == 201
    response = client.get(
        "/t/" + link["id"] + "?source=search&aff_sub1=placement", follow_redirects=False
    )
    click = parse_qs(urlsplit(response.headers["location"]).query)["click_id"][0]
    conversion = postback(client, campaign, click)
    assert conversion.status_code == 201, conversion.text
    rows = client.get("/conversions", headers=pub).json()
    assert rows[0]["payout"] == "8.0000"
    assert "revenue" not in client.get(path, headers=pub).json()[0]
    assert (
        client.post(
            path,
            headers=adv,
            json={
                **body,
                "conditions": {"all": [{"field": "ctit", "operator": "gt", "value": ["1"]}]},
            },
        ).status_code
        == 422
    )


def test_direct_capture_consent_and_origin_and_impression_accounting(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    endpoint = "/capture/" + link["id"]
    params = {"page_url": "https://brand.example/offer"}
    assert (
        client.get(endpoint, params=params, headers={"Origin": "https://brand.example"}).status_code
        == 422
    )
    assert (
        client.get(
            endpoint,
            params={**params, "consent": "true"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    response = client.get(
        endpoint, params={**params, "consent": "true"}, headers={"Origin": "https://brand.example"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == "https://brand.example"
    assert len(response.json()["click_id"]) == 32
    pixel = client.get("/i/" + link["id"] + "?source=banner")
    assert pixel.status_code == 200 and pixel.headers["content-type"] == "image/gif"
    assert len(client.get("/reports/impressions", headers=pub).json()) == 1
    assert len(client.get("/reports/clicks", headers=pub).json()) == 2


def test_partner_api_access_and_scopes_and_revocation(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    body = {"name": "Read reports", "scopes": ["reports:read"]}
    assert client.post("/api-keys", headers=pub, json=body).status_code == 403
    assert (
        client.put(
            "/users/" + publisher["id"] + "/api-access", headers=admin, json={"enabled": True}
        ).status_code
        == 200
    )
    response = client.post("/api-keys", headers=pub, json=body)
    assert response.status_code == 201, response.text
    key = response.json()
    headers = {"Authorization": "Bearer " + key["key"]}
    assert client.get("/reports/clicks", headers=headers).status_code == 200
    assert client.get("/campaigns", headers=headers).status_code == 403
    assert client.post("/api-keys", headers=headers, json=body).status_code == 403
    assert "key" not in client.get("/api-keys", headers=pub).json()[0]
    assert client.delete("/api-keys/" + key["id"], headers=pub).status_code == 200
    assert client.get("/reports/clicks", headers=headers).status_code == 401


def test_provider_drafts_are_owner_scoped_and_never_active(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    spec = client.get("/integrations/catalog", headers=adv).json()[0]
    configuration = {k: "draft" for k in spec["fields"]}
    secrets = {k: "fake-credential-for-test" for k in spec["secrets"]}
    response = client.put(
        "/integrations/" + spec["provider"],
        headers=adv,
        json={"configuration": configuration, "secrets": secrets},
    )
    assert response.status_code == 200, response.text
    assert response.json()["active"] is False and "fake-credential" not in response.text
    assert response.json()["status"] == "verification_required"
    assert client.get("/integrations", headers=pub).json() == []
    assert (
        client.put(
            "/integrations/" + spec["provider"],
            headers=adv,
            json={"configuration": {"unknown": "x"}},
        ).status_code
        == 422
    )


def test_time_cap_backfills_existing_click_and_rolls_back_rejection(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    response = client.post(
        f"/campaigns/{campaign['id']}/caps",
        headers=adv,
        json={
            "name": "Daily two",
            "metric": "clicks",
            "period": "day",
            "timezone": "Asia/Kolkata",
            "amount": "2",
        },
    )
    assert response.status_code == 201, response.text
    assert client.get("/t/" + link["id"], follow_redirects=False).status_code == 302
    assert client.get("/t/" + link["id"], follow_redirects=False).status_code == 429
    assert len(client.get("/reports/clicks", headers=pub).json()) == 2
    caps = client.get(f"/campaigns/{campaign['id']}/caps", headers=adv).json()
    assert caps[0]["recorded_usage"] == "2.0000"


def test_private_catalog_profile_and_safe_destination_templates(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    path = f"/campaigns/{campaign['id']}/profile"
    body = {
        "name": "Private",
        "description": "",
        "destination_url": "https://brand.example/offer?source={source}",
        "settings": {
            "visibility": "private",
            "internal_notes": "Only managers",
            "deep_link_enabled": True,
            "deep_link_hosts": ["brand.example"],
        },
    }
    result = client.put(path, headers=adv, json=body)
    assert result.status_code == 200, result.text
    assert "internal_notes" not in client.get(path, headers=pub).json()["settings"]
    outsider, other = partner(client, admin, "publisher", "outsider")
    assert client.get(path, headers=other).status_code == 404
    assert client.get("/campaigns", headers=other).json() == []
    assert (
        client.get(
            "/t/" + link["id"] + "?url=https://evil.example", follow_redirects=False
        ).status_code
        == 422
    )
    assert (
        client.put(
            path, headers=adv, json={**body, "destination_url": "https://brand.example/{HOST}"}
        ).status_code
        == 422
    )


def test_import_preview_and_atomic_validation(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    content = "name,destination_url,revenue,payout\nImported,https://brand.example/landing,5,3\n"
    response = client.post("/tools/import-campaigns", headers=adv, json={"csv_content": content})
    assert response.status_code == 200 and response.json()["can_import"], response.text
    assert len(client.get("/campaigns", headers=adv).json()) == 1
    response = client.post(
        "/tools/import-campaigns", headers=adv, json={"csv_content": content, "dry_run": False}
    )
    assert response.status_code == 200, response.text
    assert len(client.get("/campaigns", headers=adv).json()) == 2
    invalid = content + "Invalid,http://insecure.example,5,3\n"
    assert (
        client.post(
            "/tools/import-campaigns", headers=adv, json={"csv_content": invalid, "dry_run": False}
        ).status_code
        == 422
    )
    assert len(client.get("/campaigns", headers=adv).json()) == 2


def test_employee_permissions_narrow_existing_role(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    employee, staff = partner(client, admin, "staff", "employee")
    response = client.put(
        "/users/" + employee["id"] + "/permissions",
        headers=admin,
        json={"permissions": ["reports:read"]},
    )
    assert response.status_code == 200, response.text
    assert client.get("/reports/clicks", headers=staff).status_code == 200
    assert client.get("/campaigns", headers=staff).status_code == 403
    assert (
        client.put(
            "/users/" + employee["id"] + "/permissions", headers=staff, json={"permissions": []}
        ).status_code
        == 403
    )
    assert client.get("/notifications", headers=staff).status_code == 200


def test_campaign_callbacks_override_global_and_test_is_not_payable(client, monkeypatch):
    from app import callbacks
    from app.db import SessionLocal
    from app.models import WebhookEndpoint, OutboxEvent, CommissionEntry
    from app.services import decrypt
    from sqlalchemy import select
    import json

    monkeypatch.setattr(callbacks, "validate_destination", lambda url: None)
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    result = client.put(
        "/callbacks",
        headers=pub,
        json={"url": "https://callbacks.example/global?token=private-test-value"},
    )
    assert result.status_code == 200, result.text
    assert "private-test-value" not in result.text
    specific = client.put(
        "/callbacks",
        headers=pub,
        json={
            "url": "https://callbacks.example/campaign?click={click_id}&value={payout}",
            "offer_id": campaign["id"],
            "method": "GET",
        },
    )
    assert specific.status_code == 200, specific.text
    specific_id = specific.json()["id"]
    test = client.post("/callbacks/" + specific_id + "/test", headers=pub)
    assert test.status_code == 202 and not test.json()["financial_records_created"]
    assert client.get("/conversions", headers=pub).json() == []
    conversion = postback(client, campaign, click).json()
    assert (
        client.patch(
            "/conversions/" + conversion["id"], headers=adv, json={"status": "approved"}
        ).status_code
        == 200
    )
    with SessionLocal() as db:
        endpoint = db.get(WebhookEndpoint, specific_id)
        assert endpoint.url.startswith("enc:v1:") and "callbacks.example" not in endpoint.url
        events = list(db.scalars(select(OutboxEvent).where(OutboxEvent.kind == "webhook")))
        assert len(events) == 2
        assert all(json.loads(decrypt(e.payload))["endpoint_id"] == specific_id for e in events)
        assert len(list(db.scalars(select(CommissionEntry)))) == 1
    outsider, other = partner(client, admin, "publisher", "callback-outsider")
    assert client.get("/callback-deliveries", headers=other).json() == []
    assert client.post("/callbacks/" + specific_id + "/test", headers=other).status_code == 404
    assert (
        client.post("/callback-deliveries/" + test.json()["id"] + "/retry", headers=pub).status_code
        == 409
    )


def test_saved_reports_and_notifications_owner_isolation(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    response = client.post(
        "/report-templates",
        headers=pub,
        json={"name": "My source report", "dataset": "grouped", "group_by": "source"},
    )
    assert response.status_code == 201, response.text
    template = response.json()
    assert (
        client.get("/report-templates/" + template["id"] + "/run", headers=pub).status_code == 200
    )
    assert (
        client.get("/report-templates/" + template["id"] + "/run", headers=adv).status_code == 404
    )
    inbox = client.get("/notifications", headers=pub)
    assert inbox.status_code == 200 and len(inbox.json()) >= 1
    assert (
        client.post("/notifications/" + inbox.json()[0]["id"] + "/read", headers=adv).status_code
        == 404
    )


def test_smart_link_only_routes_approved_active_children(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    response = client.post(
        "/campaigns",
        headers=adv,
        json={
            "name": "Second",
            "destination_url": "https://brand.example/second",
            "revenue": "2",
            "payout": "1",
        },
    )
    assert response.status_code == 201, response.text
    second = response.json()
    application = client.post(f"/campaigns/{second['id']}/apply", headers=pub, json={}).json()
    assert (
        client.patch(
            "/campaign-applications/" + application["id"], headers=adv, json={"status": "approved"}
        ).status_code
        == 200
    )
    other_link = client.post(f"/campaigns/{second['id']}/link", headers=pub).json()
    smart = client.post(
        "/smart-links",
        headers=pub,
        json={
            "name": "Brand rotation",
            "children": [
                {"link_id": link["id"], "weight": 1},
                {"link_id": other_link["id"], "weight": 1},
            ],
        },
    )
    assert smart.status_code == 201, smart.text
    assert (
        client.patch(
            "/campaigns/" + campaign["id"], headers=adv, json={"active": False}
        ).status_code
        == 200
    )
    result = client.get("/s/" + smart.json()["id"] + "?source=rotation", follow_redirects=False)
    assert result.status_code == 302 and other_link["id"] in result.headers["location"]
    assert (
        client.patch("/campaigns/" + second["id"], headers=adv, json={"active": False}).status_code
        == 200
    )
    assert client.get("/s/" + smart.json()["id"], follow_redirects=False).status_code == 410


def test_automation_pauses_and_does_not_repeat(client):
    from app.db import SessionLocal
    from app.network_tools import run_automations
    from app.network_models import AutomationLog
    from sqlalchemy import select

    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    response = client.post(
        "/tools/automation",
        headers=adv,
        json={
            "name": "Low conversion rate",
            "offer_id": campaign["id"],
            "minimum_clicks": 1,
            "metric": "conversion_rate",
            "comparison": "lt",
            "threshold": "5",
            "action": "pause",
        },
    )
    assert response.status_code == 201, response.text
    with SessionLocal.begin() as db:
        run_automations(db)
    assert client.get("/campaigns", headers=adv).json()[0]["active"] is False
    with SessionLocal.begin() as db:
        run_automations(db)
        assert len(list(db.scalars(select(AutomationLog)))) == 1


def test_bulk_review_rolls_back_if_any_conversion_is_reserved(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    first = postback(client, campaign, click).json()
    goal(client, adv, campaign)
    second = postback(client, campaign, click, "second", event_key="qualified").json()
    assert (
        client.post(
            "/tools/conversions/status",
            headers=adv,
            json={"conversion_ids": [first["id"], second["id"]], "status": "approved"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/publisher-invoices",
            headers=pub,
            json={"number": "Reserved", "conversion_ids": [first["id"]]},
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/tools/conversions/status",
            headers=adv,
            json={"conversion_ids": [first["id"], second["id"]], "status": "rejected"},
        ).status_code
        == 409
    )
    assert all(c["status"] == "approved" for c in client.get("/conversions", headers=pub).json())


def test_get_callback_signature_encodes_macros_and_pins_tls(monkeypatch):
    import hashlib
    import hmac
    from types import SimpleNamespace
    from app import delivery
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "outbound_allowed_hosts", ["publisher.example"])
    captured = {}

    class Connection:
        def __init__(self, host, port, timeout):
            captured["host"] = host

        def request(self, method, path, body, headers):
            captured.update(method=method, path=path, body=body, headers=headers)

        def getresponse(self):
            return SimpleNamespace(status=204, read=lambda n: b"")

        def close(self):
            pass

    def connect(address, timeout):
        captured["address"] = address
        return object()

    def wrap(sock, server_hostname):
        captured["tls_hostname"] = server_hostname
        return object()

    monkeypatch.setattr(
        delivery,
        "socket",
        SimpleNamespace(
            SOCK_STREAM=1,
            getaddrinfo=lambda *a, **kw: [(None, None, None, None, ("93.184.216.34", 443))],
            create_connection=connect,
        ),
    )
    monkeypatch.setattr(
        delivery,
        "ssl",
        SimpleNamespace(create_default_context=lambda: SimpleNamespace(wrap_socket=wrap)),
    )
    monkeypatch.setattr(
        delivery, "http", SimpleNamespace(client=SimpleNamespace(HTTPSConnection=Connection))
    )
    assert (
        delivery.deliver_webhook(
            "https://publisher.example/event?ref={aff_click_id}",
            "test-signing-secret",
            "event1",
            {"tracking": {"aff_click_id": "a&injected=bad"}, "version": 2},
            method="GET",
        )
        == 204
    )
    assert captured["body"] is None and captured["method"] == "GET"
    query = parse_qs(urlsplit(captured["path"]).query)
    assert query["ref"] == ["a&injected=bad"] and "injected" not in query
    assert (
        captured["address"] == ("93.184.216.34", 443)
        and captured["tls_hostname"] == "publisher.example"
    )
    signature = hmac.new(
        b"test-signing-secret",
        captured["headers"]["X-Webhook-Timestamp"].encode() + b".GET\n" + captured["path"].encode(),
        hashlib.sha256,
    ).hexdigest()
    assert signature == captured["headers"]["X-Webhook-Signature"]
    assert captured["headers"]["X-Webhook-Signature-Version"] == "2"


def test_hourly_cap_distinguishes_repeated_dst_hours():
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.period_caps import bucket

    cap = SimpleNamespace(timezone="America/New_York", period="hour")
    first = bucket(cap, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
    second = bucket(cap, datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
    assert first[0] != second[0] and first[2] == second[1]
