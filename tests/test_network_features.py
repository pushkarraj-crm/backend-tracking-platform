"""Network extensions: financial idempotency, isolation and safe configuration."""

from urllib.parse import parse_qs, urlsplit

import pytest
from test_api import client as api_client  # noqa: F401
from test_api import register
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


def test_campaign_clone_is_paused_isolated_and_has_fresh_credentials(client):
    from app.core.database import SessionLocal
    from app.core.security import key_hash
    from app.persistence.models import Offer

    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    original_event = goal(client, adv, campaign)
    route = f"/campaigns/{campaign['id']}/clone"
    assert client.post(route, headers=pub, json={"name": "Forbidden"}).status_code == 403
    _, other_adv = partner(client, admin, "advertiser", "other-advertiser")
    assert client.post(route, headers=other_adv, json={"name": "Forbidden"}).status_code == 404
    response = client.post(route, headers=adv, json={"name": "Campaign copy"})
    assert response.status_code == 201, response.text
    clone = response.json()
    assert clone["id"] != campaign["id"] and clone["active"] is False
    assert clone["postback_key"] != campaign["postback_key"]
    events = client.get(f"/campaigns/{clone['id']}/events", headers=adv).json()
    assert len(events) == 1 and events[0]["code"] == "qualified"
    assert events[0]["id"] != original_event["id"]
    assert client.post(f"/campaigns/{clone['id']}/link", headers=pub).status_code != 201
    with SessionLocal() as db:
        copied = db.get(Offer, clone["id"])
        assert copied.postback_key_hash == key_hash(clone["postback_key"])
        assert copied.advertiser_id == advertiser["id"]
    # A different tenant cannot clone or discover the source.
    other, _ = register(client, "Other network")
    assert client.post(route, headers=other, json={"name": "Forbidden"}).status_code == 404


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
    import json

    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.core.services import decrypt
    from app.features.callbacks import router as callbacks
    from app.persistence.models import CommissionEntry, OutboxEvent, WebhookEndpoint

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
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.features.tools.automation import run_automations
    from app.persistence.network_models import AutomationLog

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

    from app.core.config import get_settings
    from app.features.callbacks import delivery as delivery

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

    from app.features.campaigns.caps import bucket

    cap = SimpleNamespace(timezone="America/New_York", period="hour")
    first = bucket(cap, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
    second = bucket(cap, datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
    assert first[0] != second[0] and first[2] == second[1]


def test_campaign_creation_saves_configuration_atomically(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    body = {
        "name": "Configured campaign",
        "destination_url": "https://brand.example/?click={click_id}",
        "campaign_model": "CPL",
        "revenue": "10",
        "payout": "5",
        "active": False,
        "settings": {
            "visibility": "open",
            "categories": ["Ecommerce"],
            "preview_url": "https://brand.example/preview",
            "internal_notes": "Manager only",
        },
        "tracking_rules": {"countries": ["IN", "US"]},
    }
    response = client.post("/campaigns", headers=adv, json=body)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["active"] is False
    from decimal import Decimal

    assert Decimal(created["revenue"]) == 10 and Decimal(created["payout"]) == 5
    assert created["settings"]["visibility"] == "open"
    assert client.get(f"/offers/{created['id']}/rules", headers=adv).json()["countries"] == [
        "IN",
        "US",
    ]
    assert client.post(f"/campaigns/{created['id']}/apply", headers=pub, json={}).status_code == 409
    assert (
        client.patch(f"/campaigns/{created['id']}", headers=adv, json={"active": True}).status_code
        == 200
    )
    assert (
        client.post(f"/campaigns/{created['id']}/apply", headers=pub, json={}).json()["status"]
        == "approved"
    )
    profile = client.get(f"/campaigns/{created['id']}/profile", headers=pub).json()
    assert "internal_notes" not in profile["settings"]
    assert profile["settings"]["categories"] == ["Ecommerce"]
    before = len(client.get("/campaigns", headers=adv).json())
    body["tracking_rules"] = {"countries": ["ZZ"]}
    assert client.post("/campaigns", headers=adv, json=body).status_code == 422
    assert len(client.get("/campaigns", headers=adv).json()) == before


def test_campaign_configuration_can_be_edited_without_resetting_access_or_caps(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    base = f"/campaigns/{campaign['id']}"
    fixtures = [
        (
            "commission-rules",
            {"name": "Rate", "payout": "2", "revenue": "4", "conditions": {"all": []}},
            {
                "name": "Updated rate",
                "payout": "3",
                "revenue": "6",
                "conditions": {"all": [{"field": "source", "operator": "eq", "value": "search"}]},
            },
        ),
        (
            "destinations",
            {"name": "Page", "url": "https://brand.example/one"},
            {
                "name": "Updated page",
                "url": "https://brand.example/two",
                "weight": 3,
                "countries": ["US"],
            },
        ),
        (
            "creatives",
            {"name": "Banner", "url": "https://brand.example/one.png"},
            {"name": "Updated banner", "url": "https://brand.example/two.png"},
        ),
        (
            "traffic-rules",
            {"name": "Rule", "conditions": {"all": []}, "action": "allow"},
            {
                "name": "Updated rule",
                "conditions": {"all": [{"field": "source", "operator": "eq", "value": "bad"}]},
                "action": "reject",
            },
        ),
        (
            "caps",
            {"name": "Clicks", "metric": "clicks", "amount": "10"},
            {"name": "More clicks", "metric": "clicks", "amount": "20"},
        ),
    ]
    for resource, original, updated in fixtures:
        response = client.post(f"{base}/{resource}", headers=adv, json=original)
        assert response.status_code == 201, response.text
        row = response.json()
        route = f"{base}/{resource}/{row['id']}"
        assert client.patch(route, headers=adv, json={"active": False}).status_code == 200
        assert client.put(route, headers=pub, json=updated).status_code == 403
        response = client.put(route, headers=adv, json=updated)
        assert response.status_code == 200, response.text
        assert response.json()["name"] == updated["name"]
        assert response.json()["active"] is False
        other_campaign = client.post(
            "/campaigns",
            headers=adv,
            json={
                "name": "Other",
                "destination_url": "https://brand.example",
                "revenue": "2",
                "payout": "1",
            },
        ).json()
        assert (
            client.put(
                f"/campaigns/{other_campaign['id']}/{resource}/{row['id']}",
                headers=adv,
                json=updated,
            ).status_code
            == 404
        )
    caps = client.get(f"{base}/caps", headers=adv).json()
    assert caps[0]["recorded_usage"] == "1" and caps[0]["alert_percent"] == 80
    assert (
        client.put(
            f"{base}/caps/{caps[0]['id']}",
            headers=adv,
            json={"name": "Different metric", "metric": "conversions", "amount": "20"},
        ).status_code
        == 409
    )
    outsiders, _ = register(client, "Other tenant")
    assert client.put(route, headers=outsiders, json=updated).status_code == 404


def test_configured_cap_threshold_notifies_once_when_crossed(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    profile_path = f"/campaigns/{campaign['id']}/profile"
    profile = {
        "name": campaign["name"],
        "destination_url": campaign["destination_url"],
        "settings": {"cap_alert_percent": 75},
    }
    assert client.put(profile_path, headers=adv, json=profile).status_code == 200
    assert (
        client.put(
            profile_path, headers=adv, json={**profile, "settings": {"cap_alert_percent": 0}}
        ).status_code
        == 422
    )
    cap = client.post(
        f"/campaigns/{campaign['id']}/caps",
        headers=adv,
        json={"name": "Four clicks", "metric": "clicks", "amount": "4"},
    )
    assert cap.status_code == 201, cap.text

    def notices():
        return [
            n
            for n in client.get("/notifications", headers=adv).json()
            if n["kind"] == "campaign.cap_threshold"
        ]

    for expected in [0, 1, 1]:
        assert client.get("/t/" + link["id"], follow_redirects=False).status_code == 302
        assert len(notices()) == expected
    assert client.get("/t/" + link["id"], follow_redirects=False).status_code == 429
    assert len(notices()) == 1

    # Raising the limit keeps prior usage; it must not grant a fresh empty bucket.
    cap_id = cap.json()["id"]
    assert (
        client.put(
            f"/campaigns/{campaign['id']}/caps/{cap_id}",
            headers=adv,
            json={"name": "Six clicks", "metric": "clicks", "amount": "6"},
        ).status_code
        == 200
    )
    assert client.get("/t/" + link["id"], follow_redirects=False).status_code == 302
    from decimal import Decimal

    usage = client.get(f"/campaigns/{campaign['id']}/caps", headers=adv).json()[0]["recorded_usage"]
    assert Decimal(usage) == 5


def test_postback_receipts_filter_by_campaign_and_keep_actor_scope(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    assert postback(client, campaign, click).status_code == 201
    other = client.post(
        "/campaigns",
        headers=adv,
        json={
            "name": "Other campaign",
            "destination_url": "https://brand.example/other",
            "revenue": "4",
            "payout": "2",
        },
    ).json()
    response = client.get("/postback-receipts", headers=adv, params={"offer_id": campaign["id"]})
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1 and response.json()[0]["offer_id"] == campaign["id"]
    assert (
        client.get("/postback-receipts", headers=adv, params={"offer_id": other["id"]}).json() == []
    )
    assert (
        client.get(
            "/postback-receipts", headers=pub, params={"offer_id": campaign["id"]}
        ).status_code
        == 403
    )
    _, outsider = partner(client, admin, "advertiser", "other-advertiser")
    assert (
        client.get(
            "/postback-receipts", headers=outsider, params={"offer_id": campaign["id"]}
        ).status_code
        == 404
    )
    assert (
        client.get("/postback-receipts", headers=adv, params={"offer_id": "invalid"}).status_code
        == 422
    )


def test_mis_import_matches_reference_or_click_and_is_idempotent(client):
    from app.core.database import SessionLocal
    from app.persistence.models import Conversion

    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    goal(client, adv, campaign, code="disbursed", name="Loan disbursed", payout="500")
    assert postback(client, campaign, click, "APP1").status_code == 201
    second = client.get("/t/" + link["id"], follow_redirects=False)
    click2 = parse_qs(urlsplit(second.headers["location"]).query)["click_id"][0]
    content = (
        "Application No,Status,Sub ID\n"
        "APP1,Disbursed,\n"
        "APP2,Rejected,\n"
        "APP3,In progress,\n"
        f"APP4,Approved,{click2}\n"
    )
    body = {
        "campaign_id": campaign["id"],
        "csv_content": content,
        "reference_column": "Application No",
        "status_column": "Status",
        "click_id_column": "Sub ID",
        "statuses": {
            "Disbursed": {"event_key": "disbursed", "status": "approved"},
            "Rejected": {"status": "rejected"},
            "Approved": {"status": "approved"},
        },
    }
    route = "/tools/conversions/import"
    assert client.post(route, headers=pub, json=body).status_code in {403, 404}
    _, other_adv = partner(client, admin, "advertiser", "other-advertiser")
    assert client.post(route, headers=other_adv, json=body).status_code == 404

    def stored():
        with SessionLocal() as db:
            return sorted(
                (c.transaction_id, c.event_key, c.status)
                for c in db.query(Conversion).filter_by(offer_id=campaign["id"])
            )

    before = stored()
    preview = client.post(route, headers=adv, json=body)
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert (result["created"], result["ignored"], result["leads_approved"]) == (2, 1, 1)
    assert result["errors"] == []
    assert result["unmatched"] == [{"row": 3, "reference": "APP2"}]
    assert stored() == before
    applied = client.post(route, headers=adv, json={**body, "dry_run": False})
    assert applied.status_code == 200 and applied.json()["created"] == 2, applied.text
    assert stored() == [
        ("APP1", "disbursed", "approved"),
        ("APP1", "initial", "approved"),
        ("APP4", "initial", "approved"),
    ]
    again = client.post(route, headers=adv, json={**body, "dry_run": False}).json()
    assert (again["created"], again["unchanged"]) == (0, 2)
    rejected = {**body, "csv_content": "Application No,Status,Sub ID\nAPP1,Rejected,\n"}
    assert (
        client.post(route, headers=adv, json={**rejected, "dry_run": False}).json()["updated"] == 1
    )
    again = client.post(route, headers=adv, json={**body, "dry_run": False}).json()
    assert (again["unchanged"], again["leads_approved"]) == (2, 0)
    invalid = {**body, "csv_content": content + "APP5,Approved,not-a-click\n"}
    response = client.post(route, headers=adv, json={**invalid, "dry_run": False})
    assert response.status_code == 422 and response.json()["detail"]["errors"][0]["row"] == 6
    assert stored() == [
        ("APP1", "disbursed", "approved"),
        ("APP1", "initial", "rejected"),
        ("APP4", "initial", "approved"),
    ]


def test_global_postback_resolves_campaign_from_click_and_updates_status(client):
    from app.core.database import SessionLocal
    from app.persistence.models import Conversion

    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    goal(client, adv, campaign, code="disbursed", name="Loan disbursed", payout="500")
    assert client.get("/postback-settings", headers=pub).status_code == 403
    url = client.get("/postback-settings", headers=admin).json()["url"]
    assert client.get("/postback-settings", headers=admin).json()["url"] == url
    path = "/pb/" + url.rsplit("/pb/", 1)[1]

    response = client.get(path, params={"click_id": click, "txn_id": "APP9", "event": "lead"})
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "pending"
    again = client.get(path, params={"CLICK_ID": click, "TXN_ID": "APP9", "event": "lead"})
    assert again.status_code == 200 and again.json()["duplicate"] is True
    # A CPL event ignores the amount other platforms send with every postback.
    stage = client.post(
        path,
        json={
            "click_id": click,
            "txn_id": "APP9",
            "event": "disbursed",
            "amount": "250000",
            "currency": "XYZ",
            "status": "approved",
            "coupon": "PRIYA10",
        },
    )
    assert stage.status_code == 201 and stage.json()["status"] == "approved", stage.text
    with SessionLocal() as db:
        rows = {
            c.event_key: (c.status, c.advertiser_parameters)
            for c in db.query(Conversion).filter_by(offer_id=campaign["id"])
        }
    assert rows["initial"][0] == "approved"
    assert rows["disbursed"] == ("approved", {"coupon": "PRIYA10"})

    assert client.get(path, params={"click_id": "f" * 32}).status_code == 404
    assert client.get(path, params={"click_id": click, "status": "paid"}).status_code == 422
    assert client.get("/pb/" + "x" * 32, params={"click_id": click}).status_code == 401
    assert client.post("/postback-settings/rotate", headers=adv).status_code == 403
    rotated = client.post("/postback-settings/rotate", headers=admin).json()["url"]
    assert rotated != url
    assert client.get(path, params={"click_id": click, "txn_id": "APP9"}).status_code == 401


def test_link_forwards_personal_parameters_without_storing_them(client):
    from app.core.database import SessionLocal
    from app.persistence.models import Click

    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    response = client.get(
        "/t/" + link["id"],
        params={
            "deviceid": "dev-1",
            "googleaid": "gaid-1",
            "t_email": "a@b.example",
            "t_sub3": "x",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["t_email"] == ["a@b.example"] and params["deviceid"] == ["dev-1"]
    assert params["t_sub3"] == ["x"]
    with SessionLocal() as db:
        stored = db.get(Click, params["click_id"][0]).attributes
    assert stored["deviceid"] == "dev-1" and stored["googleaid"] == "gaid-1"
    assert not any(key.startswith("t_") for key in stored)


def test_brand_account_is_its_own_advertiser(client):
    from test_api import signup_body

    response = client.post(
        "/auth/register",
        json={**signup_body("Brand Co"), "business_type": "brand_direct_advertiser"},
    )
    assert response.status_code == 201, response.text
    brand = {"Authorization": "Bearer " + response.json()["access_token"]}
    assert client.get("/workspace", headers=brand).json()["account_type"] == "brand"
    body = {
        "name": "Diwali sale",
        "destination_url": "https://brand.example/sale",
        "revenue": "80",
        "payout": "80",
        "campaign_model": "CPL",
    }
    first = client.post("/campaigns", headers=brand, json=body)
    second = client.post("/campaigns", headers=brand, json={**body, "name": "Second"})
    assert first.status_code == 201 and second.status_code == 201, first.text
    assert first.json()["advertiser_id"] == second.json()["advertiser_id"]
    advertiser = {
        "email": "adv@brand.example",
        "password": "affiliate-password-123",
        "role": "advertiser",
    }
    assert client.post("/users", headers=brand, json=advertiser).status_code == 403
    agency = {**advertiser, "email": "agency@brand.example", "role": "affiliate"}
    assert client.post("/users", headers=brand, json=agency).status_code == 201
    roles = sorted(u["role"] for u in client.get("/users", headers=brand).json())
    assert roles == ["admin", "affiliate"]


def test_targets_track_progress_without_blocking_and_include_agency_publishers(client):
    from datetime import datetime, timedelta, timezone

    from app.core.database import SessionLocal
    from app.persistence.models import User

    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    agency, _ = partner(client, admin, "affiliate", "agency")
    with SessionLocal() as db:
        db.get(User, publisher["id"]).parent_user_id = agency["id"]
        db.commit()
    today = datetime.now(timezone.utc).date()  # the server counts days in UTC
    window = {
        "starts_on": str(today - timedelta(days=4)),
        "ends_on": str(today + timedelta(days=5)),
    }
    route = f"/campaigns/{campaign['id']}/targets"
    overall = client.post(route, headers=adv, json={"name": "Overall", "amount": 10, **window})
    split = client.post(
        route,
        headers=adv,
        json={"name": "Agency A", "amount": 1, "partner_id": agency["id"], **window},
    )
    assert overall.status_code == 201 and split.status_code == 201, split.text
    assert client.post(
        route, headers=pub, json={"name": "x", "amount": 1, **window}
    ).status_code in {403, 404}
    bad = {**window, "ends_on": str(today - timedelta(days=9))}
    assert (
        client.post(route, headers=adv, json={"name": "Bad", "amount": 1, **bad}).status_code == 422
    )

    assert postback(client, campaign, click, "T1").status_code == 201
    second = client.get("/t/" + link["id"], follow_redirects=False)
    click2 = parse_qs(urlsplit(second.headers["location"]).query)["click_id"][0]
    # Targets never block: the agency's second sale goes past its target of 1.
    assert postback(client, campaign, click2, "T2").status_code == 201
    rows = {r["name"]: r for r in client.get(route, headers=adv).json()}
    assert rows["Overall"]["achieved"] == 2 and rows["Overall"]["expected_by_today"] == 5
    assert rows["Overall"]["pace"] == "behind" and rows["Overall"]["commission"] == "8.5000"
    assert rows["Agency A"]["achieved"] == 2 and rows["Agency A"]["percent"] == 200
    assert rows["Agency A"]["pace"] == "ahead"
    summary = client.get("/targets", headers=admin).json()
    assert {r["campaign_name"] for r in summary} == {campaign["name"]} and len(summary) == 2


def test_offer18_style_links_use_stable_public_numbers(client):
    import re

    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    url = link["url"]
    match = re.search(r"/c\?o=(\d{6})&a=(\d{6})$", url)
    assert match, url
    again = client.post(f"/campaigns/{campaign['id']}/link", headers=pub).json()["url"]
    listed = [r["url"] for r in client.get("/links", headers=pub).json()]
    assert again == url and listed == [url]
    response = client.get(
        "/c", params={"o": match[1], "a": match[2], "aff_click_id": "net-9"}, follow_redirects=False
    )
    assert response.status_code == 302, response.text
    assert parse_qs(urlsplit(response.headers["location"]).query)["aff_click_id"] == ["net-9"]
    assert client.get("/c", params={"o": match[1], "a": 1}).status_code == 404
    assert client.get("/t/" + link["id"], follow_redirects=False).status_code == 302


def test_influencer_tracks_brand_deals_by_post_platform_and_manual_totals(client):
    from datetime import datetime, timezone

    from test_api import signup_body

    response = client.post(
        "/auth/register",
        json={**signup_body("Priya Creates"), "business_type": "influencer_creator"},
    )
    assert response.status_code == 201, response.text
    me = {"Authorization": "Bearer " + response.json()["access_token"]}
    assert client.get("/workspace", headers=me).json()["account_type"] == "influencer"
    brand = client.post("/brands", headers=me, json={"name": "boAt"})
    assert brand.status_code == 201, brand.text
    assert [b["name"] for b in client.get("/brands", headers=me).json()] == ["boAt"]
    deal = client.post(
        "/campaigns",
        headers=me,
        json={
            "name": "boAt Airdopes launch",
            "destination_url": "https://boat.example/airdopes",
            "revenue": "60",
            "payout": "60",
            "campaign_model": "CPL",
            "advertiser_id": brand.json()["id"],
            "currency": "INR",
        },
    )
    assert deal.status_code == 201, deal.text
    story = client.post(
        "/placements",
        headers=me,
        json={
            "campaign_id": deal.json()["id"],
            "name": "Insta story 28 Oct",
            "platform": "instagram",
        },
    ).json()
    review = client.post(
        "/placements",
        headers=me,
        json={"campaign_id": deal.json()["id"], "name": "YouTube review", "platform": "youtube"},
    ).json()
    assert story["url"] != review["url"] and "&sub_id=instagram-insta-story-28-oct-" in story["url"]

    def click(url):
        parts = urlsplit(url)
        r = client.get(parts.path + "?" + parts.query, follow_redirects=False)
        assert r.status_code == 302, r.text
        return parse_qs(urlsplit(r.headers["location"]).query)["click_id"][0]

    first, _, _ = click(story["url"]), click(story["url"]), click(review["url"])
    pb = client.get("/postback-settings", headers=me).json()["url"]
    pb_path = "/pb/" + pb.rsplit("/pb/", 1)[1]
    assert client.get(pb_path, params={"click_id": first, "txn_id": "ORD1"}).status_code == 201
    manual = client.post(
        "/manual-results",
        headers=me,
        json={
            "campaign_id": deal.json()["id"],
            "period_start": str(datetime.now(timezone.utc).date()),
            "period_end": str(datetime.now(timezone.utc).date()),
            "sales": 3,
            "earnings": "180",
            "note": "Brand email, coupon PRIYA10",
        },
    )
    assert manual.status_code == 201, manual.text
    summary = client.get("/influencer/summary", headers=me).json()
    assert summary["deals"][0]["brand"] == "boAt"
    assert (summary["deals"][0]["clicks"], summary["deals"][0]["sales"]) == (3, 4)
    assert summary["deals"][0]["earnings"] == "240.0000"
    spots = {p["name"]: p for p in summary["placements"]}
    assert (spots["Insta story 28 Oct"]["clicks"], spots["Insta story 28 Oct"]["sales"]) == (2, 1)
    assert spots["Insta story 28 Oct"]["conversion_rate"] == 50
    assert (spots["YouTube review"]["clicks"], spots["YouTube review"]["sales"]) == (1, 0)
    assert {p["platform"]: p["clicks"] for p in summary["platforms"]} == {
        "instagram": 2,
        "youtube": 1,
    }
    partner = {
        "email": "pub@creator.example",
        "password": "affiliate-password-123",
        "role": "publisher",
    }
    assert client.post("/users", headers=me, json=partner).status_code == 403
    admin, *_ = campaign_flow(client)
    assert client.get("/brands", headers=admin).status_code == 404


def test_campaign_created_with_funnel_stages_in_one_step(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    body = {
        "name": "Personal loan funnel",
        "destination_url": "https://lender.example/apply?sub={click_id}",
        "revenue": "60",
        "payout": "50",
        "campaign_model": "CPL",
        "currency": "INR",
        "attribution_hours": 1440,
        "settings": {"visibility": "approval", "categories": ["Loans"], "external_id": "LN-77"},
        "tracking_rules": {"countries": ["IN"], "daily_conversions": 500},
        "events": [
            {"code": "approved", "name": "Loan approved", "payout": "200", "revenue": "250"},
            {"code": "disbursed", "name": "Loan disbursed", "payout": "500", "revenue": "650"},
        ],
    }
    created = client.post("/campaigns", headers=adv, json=body)
    assert created.status_code == 201, created.text
    events = client.get(f"/campaigns/{created.json()['id']}/events", headers=adv).json()
    assert sorted((e["code"], e["payout"]) for e in events) == [
        ("approved", "200.0000"),
        ("disbursed", "500.0000"),
    ]
    before = len(client.get("/campaigns", headers=adv).json())
    duplicate = {**body, "events": [body["events"][0], body["events"][0]]}
    assert client.post("/campaigns", headers=adv, json=duplicate).status_code == 422
    initial = {**body, "events": [{"code": "initial", "name": "Lead"}]}
    assert client.post("/campaigns", headers=adv, json=initial).status_code == 422
    assert len(client.get("/campaigns", headers=adv).json()) == before


def test_blocked_sources_reject_clicks_and_later_conversions_and_partners_see_why(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    t = "/t/" + link["id"]
    blocks = [{"preset": "cashback_sites"}, {"preset": "meta_ads"}]
    for body in blocks:
        assert client.post("/blocked-sources", headers=admin, json=body).status_code == 201
    campaign_only = {"preset": "custom_source", "value": "Telegram", "campaign_id": campaign["id"]}
    assert client.post("/blocked-sources", headers=admin, json=campaign_only).status_code == 201
    bad = [
        {"preset": "custom_domain", "value": "not a domain"},
        {"preset": "google_ads", "value": "x"},
        {"preset": "custom_source", "value": " "},
    ]
    for body in bad:
        assert client.post("/blocked-sources", headers=admin, json=body).status_code == 422
    assert client.post("/blocked-sources", headers=pub, json=blocks[0]).status_code == 403

    cashback = client.get(
        t, headers={"Referer": "https://www.cashkaro.com/deals?x=1"}, follow_redirects=False
    )
    assert cashback.status_code == 403
    assert (
        client.get(t, params={"utm_source": "telegram"}, follow_redirects=False).status_code == 403
    )
    normal = client.get(t, headers={"Referer": "https://www.google.com/"}, follow_redirects=False)
    assert normal.status_code == 302
    assert client.get(t, params={"fbclid": "abc"}, follow_redirects=False).status_code == 403
    seen = client.get("/fraud/partner-events", headers=pub).json()
    assert {e["reason"] for e in seen} == {
        "Blocked source: Cashback and coupon sites",
        "Blocked source: source / utm_source value",
        "Blocked source: Facebook / Instagram ads (fbclid)",
    }
    assert client.get("/fraud/partner-events", headers=adv).status_code == 403
    # A click recorded before a block was added still converts, but earns nothing.
    google = client.get(t, params={"gclid": "g-1"}, follow_redirects=False)
    google_click = parse_qs(urlsplit(google.headers["location"]).query)["click_id"][0]
    assert (
        client.post("/blocked-sources", headers=admin, json={"preset": "google_ads"}).status_code
        == 201
    )
    late = postback(client, campaign, google_click, "LATE1")
    assert late.status_code == 201 and late.json()["status"] == "rejected", late.text
    # Clicks recorded before referrer capture have no referrer field, so "no referrer"
    # must not retro-block them.
    from app.core.database import SessionLocal
    from app.persistence.models import Click

    plain = parse_qs(urlsplit(normal.headers["location"]).query)["click_id"][0]
    with SessionLocal() as db:
        old = db.get(Click, plain)
        old.attributes = {k: v for k, v in old.attributes.items() if k != "referrer"}
        db.commit()
    added = client.post("/blocked-sources", headers=admin, json={"preset": "no_referrer"})
    assert added.status_code == 201
    assert postback(client, campaign, plain, "OLD1").json()["status"] == "pending"
    listed = client.get("/blocked-sources", headers=admin).json()
    assert len(listed["blocks"]) == 5 and "cashkaro.com" in listed["cashback_domains"]
    block_id = listed["blocks"][0]["id"]
    assert (
        client.patch(f"/blocked-sources/{block_id}", headers=admin, json={"active": False}).json()[
            "active"
        ]
        is False
    )


def test_unique_click_window_reuses_the_visitors_earlier_click(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    rules = client.get(f"/offers/{campaign['id']}/rules", headers=adv).json()
    saved = client.put(
        f"/offers/{campaign['id']}/rules", headers=adv, json={**rules, "unique_click_hours": 24}
    )
    assert saved.status_code == 200, saved.text

    def click_id():
        r = client.get("/t/" + link["id"], follow_redirects=False)
        assert r.status_code == 302, r.text
        return parse_qs(urlsplit(r.headers["location"]).query)["click_id"][0]

    # The fixture's click came from this same visitor within the window, so it is reused.
    assert click_id() == click and click_id() == click
    assert len(client.get("/reports/clicks", headers=pub).json()) == 1


def test_coupons_credit_sales_without_clicks_and_win_over_other_partners(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    body = {"campaign_id": campaign["id"], "partner_id": publisher["id"], "code": "priya10"}
    created = client.post("/coupons", headers=admin, json=body)
    assert created.status_code == 201 and created.json()["code"] == "PRIYA10", created.text
    assert (
        client.post("/coupons", headers=admin, json={**body, "code": "PRIYA10"}).status_code == 409
    )
    assert client.post("/coupons", headers=admin, json={**body, "code": "a b"}).status_code == 422
    assert client.post("/coupons", headers=pub, json={**body, "code": "MINE"}).status_code == 403
    stranger, _ = partner(client, admin, "publisher", "stranger")
    unapproved = {**body, "partner_id": stranger["id"], "code": "STRANGER"}
    assert client.post("/coupons", headers=admin, json=unapproved).status_code == 422

    pb = client.get("/postback-settings", headers=admin).json()["url"]
    pb = "/pb/" + pb.rsplit("/pb/", 1)[1]
    first = client.get(pb, params={"coupon": "priya10", "txn_id": "ORD1"})
    assert first.status_code == 201, first.text
    assert client.get(pb, params={"coupon": "PRIYA10", "txn_id": "ORD1"}).status_code == 200
    assert client.get(pb, params={"coupon": "PRIYA10"}).status_code == 422
    assert client.get(pb, params={"coupon": "NOPE", "txn_id": "X"}).status_code == 404

    # Another partner's click loses to the coupon's partner.
    rival, rival_headers = partner(client, admin, "publisher", "rival")
    application = client.post(
        f"/campaigns/{campaign['id']}/apply", headers=rival_headers, json={"message": "ads"}
    ).json()
    client.patch(
        f"/campaign-applications/{application['id']}", headers=adv, json={"status": "approved"}
    )
    rival_link = client.post(f"/campaigns/{campaign['id']}/link", headers=rival_headers).json()
    redirect = client.get("/t/" + rival_link["id"], follow_redirects=False)
    rival_click = parse_qs(urlsplit(redirect.headers["location"]).query)["click_id"][0]
    won = client.get(pb, params={"click_id": rival_click, "coupon": "PRIYA10", "txn_id": "ORD2"})
    assert won.status_code == 201, won.text
    mine = client.get("/coupons/mine", headers=pub).json()
    assert (mine[0]["code"], mine[0]["uses"], mine[0]["conversions"]) == ("PRIYA10", 2, 2)
    assert client.get("/coupons/mine", headers=rival_headers).json() == []

    # The quota stops further coupon credit.
    code_id = created.json()["id"]
    client.patch(f"/coupons/{code_id}", headers=admin, json={"quota": 2})
    assert client.get(pb, params={"coupon": "PRIYA10", "txn_id": "ORD3"}).status_code == 404
    client.patch(f"/coupons/{code_id}", headers=admin, json={"active": False, "quota": 5})
    assert client.get(pb, params={"coupon": "PRIYA10", "txn_id": "ORD3"}).status_code == 404

    csv_body = {"campaign_id": campaign["id"], "partner_id": publisher["id"]}
    duplicate = "code,quota\nDIWALI1,10\nDIWALI1,5\n"
    preview = client.post(
        "/coupons/import", headers=admin, json={**csv_body, "csv_content": duplicate}
    )
    assert preview.json()["errors"][0]["row"] == 3
    good = "code,quota,ends_on\nDIWALI1,10,2030-01-01\nDIWALI2,,\n"
    imported = client.post(
        "/coupons/import", headers=admin, json={**csv_body, "csv_content": good, "dry_run": False}
    )
    assert imported.status_code == 200 and imported.json()["codes"] == 2, imported.text
    assert len(client.get("/coupons", headers=admin).json()) == 3


def test_creator_coupons_belong_to_the_creator(client):
    from test_api import signup_body

    me = {
        "Authorization": "Bearer "
        + client.post(
            "/auth/register",
            json={**signup_body("Coupon Creator"), "business_type": "influencer_creator"},
        ).json()["access_token"]
    }
    brand = client.post("/brands", headers=me, json={"name": "Mamaearth"}).json()
    deal = client.post(
        "/campaigns",
        headers=me,
        json={
            "name": "Glow kit",
            "destination_url": "https://mama.example/kit",
            "revenue": "45",
            "payout": "45",
            "advertiser_id": brand["id"],
        },
    ).json()
    coupon = client.post("/coupons", headers=me, json={"campaign_id": deal["id"], "code": "GLOW10"})
    assert coupon.status_code == 201, coupon.text
    pb = client.get("/postback-settings", headers=me).json()["url"]
    sale = client.get("/pb/" + pb.rsplit("/pb/", 1)[1], params={"coupon": "glow10", "txn_id": "M1"})
    assert sale.status_code == 201, sale.text
    summary = client.get("/influencer/summary", headers=me).json()
    assert (summary["deals"][0]["sales"], summary["deals"][0]["earnings"]) == (1, "45.0000")


def test_store_sales_from_the_browser_stay_pending_and_send_cors(client):
    import json as jsonlib
    from app.core.database import SessionLocal
    from app.persistence.models import Offer

    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    with SessionLocal() as db:
        offer = db.get(Offer, campaign["id"])
        offer.catalog_settings = {**offer.catalog_settings, "initial_auto_approve": True}
        db.commit()
    settings = client.get("/web-conversion-settings", headers=admin).json()
    assert client.get("/web-conversion-settings", headers=admin).json() == settings
    assert client.get("/web-conversion-settings", headers=pub).status_code == 403
    assert settings["sdk_url"].endswith("/sdk/relay.js")
    path = "/wc/" + settings["endpoint"].rsplit("/wc/", 1)[1]
    store = {"Origin": "https://shop.example", "Content-Type": "text/plain"}

    def send(body, key_path=path):
        return client.post(key_path, headers=store, content=jsonlib.dumps(body))

    sale = send({"click_id": click, "txn_id": "SHOP-1", "status": "approved", "amount": "999"})
    assert sale.status_code == 201, sale.text
    assert sale.json()["status"] == "pending"  # browser sales wait despite auto-approve
    assert sale.headers["access-control-allow-origin"] == "https://shop.example"
    # The same campaign auto-approves server postbacks, so the pending state above is the browser rule.
    fresh = client.get("/t/" + link["id"], follow_redirects=False)
    fresh_click = parse_qs(urlsplit(fresh.headers["location"]).query)["click_id"][0]
    pb = client.get("/postback-settings", headers=admin).json()["url"]
    server = client.get(
        "/pb/" + pb.rsplit("/pb/", 1)[1], params={"click_id": fresh_click, "txn_id": "S1"}
    )
    assert server.json()["status"] == "approved", server.text
    again = send({"click_id": click, "txn_id": "SHOP-1", "amount": "999"})
    assert again.status_code == 200 and again.json()["duplicate"] is True
    missing = send({"click_id": click})
    assert missing.status_code == 422 and missing.headers["access-control-allow-origin"]
    assert send({"click_id": click, "txn_id": "X"}, "/wc/" + "z" * 24).status_code == 401
    bad = client.post(path, headers=store, content="not json")
    assert bad.status_code == 422

    coupon = {"campaign_id": campaign["id"], "partner_id": publisher["id"], "code": "SHOP10"}
    assert client.post("/coupons", headers=admin, json=coupon).status_code == 201
    by_code = send({"coupon": "shop10", "txn_id": "SHOP-2", "amount": "500"})
    assert by_code.status_code == 201 and by_code.json()["status"] == "pending", by_code.text

    sdk = client.get("/sdk/relay.js").text
    assert "remember:" in sdk and "conversion:" in sdk and "text/plain" in sdk


def _campaign_settings(campaign_id, **values):
    from app.core.database import SessionLocal
    from app.persistence.models import Offer

    with SessionLocal() as db:
        offer = db.get(Offer, campaign_id)
        offer.catalog_settings = {**offer.catalog_settings, **values}
        db.commit()


@pytest.mark.parametrize(
    "action,second",
    [
        ("refuse", 409),
        ("reject", "rejected"),
        ("pending", "pending"),
        ("accept", "approved"),
        ("stop", "pending"),
    ],
)
def test_cap_action_decides_what_happens_to_conversions_over_the_cap(client, action, second):
    from app.core.database import SessionLocal
    from app.persistence.models import Offer

    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    _campaign_settings(campaign["id"], cap_action=action, initial_auto_approve=True)
    rules = client.get(f"/offers/{campaign['id']}/rules", headers=adv).json()
    client.put(
        f"/offers/{campaign['id']}/rules", headers=adv, json={**rules, "daily_conversions": 1}
    )
    assert postback(client, campaign, click, "CAP1").json()["status"] == "approved"
    fresh = client.get("/t/" + link["id"], follow_redirects=False)
    click2 = parse_qs(urlsplit(fresh.headers["location"]).query)["click_id"][0]
    response = postback(client, campaign, click2, "CAP2")
    if second == 409:
        assert response.status_code == 409
    else:
        assert response.status_code == 201 and response.json()["status"] == second, response.text
    with SessionLocal() as db:
        assert db.get(Offer, campaign["id"]).active is (action != "stop")
    if action == "stop":
        titles = [n["title"] for n in client.get("/notifications", headers=admin).json()]
        assert any("paused: conversion cap reached" in t for t in titles)


def test_unservable_clicks_fall_back_to_the_partners_link_on_another_campaign_once(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    backup = client.post(
        "/campaigns",
        headers=adv,
        json={
            "name": "Backup",
            "destination_url": "https://brand.example/backup",
            "revenue": "1",
            "payout": "1",
        },
    ).json()
    application = client.post(
        f"/campaigns/{backup['id']}/apply", headers=pub, json={"message": "x"}
    )
    client.patch(
        f"/campaign-applications/{application.json()['id']}",
        headers=adv,
        json={"status": "approved"},
    )
    backup_link = client.post(f"/campaigns/{backup['id']}/link", headers=pub).json()
    for campaign_id in (campaign["id"], backup["id"]):
        rules = client.get(f"/offers/{campaign_id}/rules", headers=adv).json()
        client.put(f"/offers/{campaign_id}/rules", headers=adv, json={**rules, "countries": ["US"]})
    _campaign_settings(campaign["id"], fallback_campaign_id=backup["id"])
    first = client.get("/t/" + link["id"], params={"aff_sub1": "story"}, follow_redirects=False)
    assert first.status_code == 302
    hop = urlsplit(first.headers["location"])
    assert hop.path == "/t/" + backup_link["id"] and "relay_fallback=1" in hop.query
    assert "aff_sub1=story" in hop.query
    # The backup campaign is also out of targeting; the fallback is never followed twice.
    _campaign_settings(backup["id"], fallback_campaign_id=campaign["id"])
    assert client.get(hop.path + "?" + hop.query, follow_redirects=False).status_code == 403
    rules = client.get(f"/offers/{backup['id']}/rules", headers=adv).json()
    client.put(f"/offers/{backup['id']}/rules", headers=adv, json={**rules, "countries": []})
    landed = client.get(hop.path + "?" + hop.query, follow_redirects=False)
    assert landed.status_code == 302 and landed.headers["location"].startswith(
        "https://brand.example/backup"
    )
    # No hop to a backup the partner is no longer approved on.
    client.patch(
        f"/campaign-applications/{application.json()['id']}",
        headers=adv,
        json={"status": "rejected"},
    )
    assert client.get("/t/" + link["id"], follow_redirects=False).status_code == 403


def test_revenue_share_pays_the_partner_a_percentage_of_the_reported_commission(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    deal = client.post(
        "/campaigns",
        headers=adv,
        json={
            "name": "Card signups (rev share)",
            "destination_url": "https://bank.example/card",
            "campaign_model": "CPS",
            "sale_percentage": "70",
            "revenue": "0",
            "payout": "0",
            "settings": {"percent_of": "commission", "initial_repeatable": True},
        },
    )
    assert deal.status_code == 201, deal.text
    deal = deal.json()
    application = client.post(f"/campaigns/{deal['id']}/apply", headers=pub, json={}).json()
    client.patch(
        f"/campaign-applications/{application['id']}", headers=adv, json={"status": "approved"}
    )
    deal_link = client.post(f"/campaigns/{deal['id']}/link", headers=pub).json()
    hop = client.get("/t/" + deal_link["id"], follow_redirects=False)
    deal_click = parse_qs(urlsplit(hop.headers["location"]).query)["click_id"][0]
    pb = client.get("/postback-settings", headers=admin).json()["url"]
    pb = "/pb/" + pb.rsplit("/pb/", 1)[1]
    # The advertiser reports its commission as payout; the order amount is ignored.
    sale = client.get(
        pb, params={"click_id": deal_click, "txn_id": "CARD1", "payout": "1000", "amount": "99999"}
    )
    assert sale.status_code == 201, sale.text
    row = next(
        r for r in client.get("/conversions", headers=admin).json() if r["id"] == sale.json()["id"]
    )
    assert (row["revenue"], row["payout"]) == ("1000.0000", "700.0000")
    assert "revenue" not in client.get("/conversions", headers=pub).json()[0]
    missing = client.get(pb, params={"click_id": deal_click, "txn_id": "CARD2", "amount": "500"})
    assert missing.status_code == 422
    # Ordinary CPS keeps paying a percentage of the order value.
    profile = client.get(f"/campaigns/{deal['id']}/profile", headers=adv).json()
    client.put(
        f"/campaigns/{deal['id']}/profile",
        headers=adv,
        json={
            "name": profile["name"],
            "destination_url": profile["destination_url"],
            "settings": {**profile["settings"], "percent_of": "sale"},
        },
    )
    order = client.get(pb, params={"click_id": deal_click, "txn_id": "CARD3", "amount": "500"})
    assert order.status_code == 201, order.text
    row = next(
        r for r in client.get("/conversions", headers=admin).json() if r["id"] == order.json()["id"]
    )
    assert (row["revenue"], row["payout"]) == ("350.0000", "350.0000")


def _queued_alerts():
    import json

    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.core.services import decrypt
    from app.persistence.models import OutboxEvent

    with SessionLocal() as db:
        return [
            json.loads(decrypt(e.payload))
            for e in db.scalars(select(OutboxEvent).where(OutboxEvent.kind == "alert"))
        ]


SLACK_HOOK = "https://hooks.slack.com/services/T000/B000/abcdefghijklmnopqrstuvwx"
# Synthetic credential for validation and redaction tests; never contacts Telegram.
BOT_TOKEN = f"{123456789}:{'A' * 35}"


def test_alert_settings_keep_secrets_and_test_messages_never_leak_them(client, monkeypatch):
    from app.features.alerts import router as alerts

    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    assert client.get("/alert-settings", headers=pub).status_code == 403
    assert client.get("/alert-settings", headers=admin).json() == {
        "slack": False,
        "telegram": False,
        "telegram_chat_id": "",
        "events": ["caps", "postback_errors", "fraud"],
    }
    bad = client.put(
        "/alert-settings", headers=admin, json={"slack_webhook_url": "https://evil.example/x"}
    )
    assert bad.status_code == 422
    assert (
        client.put(
            "/alert-settings", headers=admin, json={"telegram_bot_token": "nope"}
        ).status_code
        == 422
    )
    saved = client.put(
        "/alert-settings",
        headers=admin,
        json={
            "slack_webhook_url": SLACK_HOOK,
            "telegram_bot_token": BOT_TOKEN,
            "telegram_chat_id": "-1001234",
        },
    )
    assert saved.status_code == 200, saved.text
    assert SLACK_HOOK not in saved.text and BOT_TOKEN not in saved.text
    assert saved.json()["slack"] and saved.json()["telegram"]
    # Omitted fields are kept; "" removes one.
    kept = client.put("/alert-settings", headers=admin, json={"events": ["fraud"]}).json()
    assert kept["slack"] and kept["telegram"] and kept["events"] == ["fraud"]
    sent = []
    monkeypatch.setattr(
        alerts, "send", lambda channel, settings, text: sent.append((channel, text))
    )
    assert client.post(
        "/alert-settings/test", headers=admin, json={"channel": "telegram"}
    ).json() == {"sent": True}
    assert sent[0][0] == "telegram" and "Test alert" in sent[0][1]

    def broken(channel, settings, text):
        raise RuntimeError(f"failed for https://api.telegram.org/bot{BOT_TOKEN}/sendMessage")

    monkeypatch.setattr(alerts, "send", broken)
    failed = client.post("/alert-settings/test", headers=admin, json={"channel": "telegram"})
    assert failed.status_code == 502 and BOT_TOKEN not in failed.text
    removed = client.put("/alert-settings", headers=admin, json={"slack_webhook_url": ""}).json()
    assert removed["slack"] is False
    assert (
        client.post("/alert-settings/test", headers=admin, json={"channel": "slack"}).status_code
        == 409
    )


def test_fraud_and_postback_errors_alert_once_per_campaign_per_hour(client, monkeypatch):
    from app.features.alerts import router as alerts

    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    client.put("/alert-settings", headers=admin, json={"slack_webhook_url": SLACK_HOOK})
    rules = client.get(f"/offers/{campaign['id']}/rules", headers=adv).json()
    client.put(
        f"/offers/{campaign['id']}/rules",
        headers=adv,
        json={**rules, "minimum_conversion_seconds": 3600},
    )
    # Too-fast conversions are fraud and also refused postbacks: one alert of each kind.
    assert postback(client, campaign, click, "FAST1").status_code == 403
    assert postback(client, campaign, click, "FAST2").status_code == 403
    texts = [a["text"] for a in _queued_alerts()]
    assert len(texts) == 2 and all(a["channel"] == "slack" for a in _queued_alerts())
    assert any("traffic blocked (conversion too fast)" in t for t in texts)
    assert any("a postback was refused (policy rejected, HTTP 403)" in t for t in texts)
    assert all(t.startswith("Relay · ") for t in texts)

    # The worker sends queued alerts through the configured channel.
    from app.worker import run_once

    sent = []
    monkeypatch.setattr(alerts, "send", lambda channel, settings, text: sent.append(channel))
    while run_once():
        pass
    assert sent == ["slack", "slack"]


def test_alerts_follow_the_chosen_events_and_cap_pause(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    client.put(
        "/alert-settings",
        headers=admin,
        json={
            "telegram_bot_token": BOT_TOKEN,
            "telegram_chat_id": "@relayalerts",
            "events": ["caps"],
        },
    )
    _campaign_settings(campaign["id"], cap_action="stop")
    rules = client.get(f"/offers/{campaign['id']}/rules", headers=adv).json()
    client.put(
        f"/offers/{campaign['id']}/rules", headers=adv, json={**rules, "daily_conversions": 1}
    )
    assert postback(client, campaign, click, "C1").status_code == 201
    fresh = client.get("/t/" + link["id"], follow_redirects=False)
    click2 = parse_qs(urlsplit(fresh.headers["location"]).query)["click_id"][0]
    assert postback(client, campaign, click2, "C2").json()["status"] == "pending"
    alerts = _queued_alerts()
    assert len(alerts) == 1 and alerts[0]["channel"] == "telegram"
    assert "is paused: its conversion cap is full" in alerts[0]["text"]
    # A paused campaign refuses clicks, but postback errors are not among the chosen events.
    assert client.get("/t/" + link["id"], follow_redirects=False).status_code == 410
    assert len(_queued_alerts()) == 1


def _audit_details(action):
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.persistence.models import AuditEvent

    with SessionLocal() as db:
        return [e.detail for e in db.scalars(select(AuditEvent).where(AuditEvent.action == action))]


def test_admin_can_log_in_as_a_partner_with_limits_and_an_audit_trail(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    route = f"/users/{publisher['id']}/login-as"
    assert client.post(route, headers=adv).status_code == 403
    me = client.get("/me", headers=admin).json()
    assert client.post(f"/users/{me['id']}/login-as", headers=admin).status_code == 404
    opened = client.post(route, headers=admin)
    assert opened.status_code == 200, opened.text
    assert opened.json()["user"]["email"] == publisher["email"]
    assert "refresh_token" not in opened.headers.get("set-cookie", "")
    as_pub = {"Authorization": "Bearer " + opened.json()["access_token"]}
    assert client.get("/me", headers=as_pub).json()["id"] == publisher["id"]
    # Account-security actions stay with the real owner.
    keys = client.post("/api-keys", headers=as_pub, json={"name": "x", "scopes": ["reports:read"]})
    assert keys.status_code == 403 and "owner" in keys.text
    assert client.post("/auth/logout-all", headers=as_pub).status_code == 403
    # What the admin does as the partner is recorded under the admin.
    other = client.post(
        "/campaigns",
        headers=adv,
        json={
            "name": "Second",
            "destination_url": "https://brand.example/2",
            "revenue": "1",
            "payout": "1",
        },
    ).json()
    assert (
        client.post(f"/campaigns/{other['id']}/apply", headers=as_pub, json={}).status_code == 201
    )
    applied = _audit_details("campaign.applied")
    assert any(f"[by admin {me['email']}]" in d for d in applied)
    assert _audit_details("user.login_as") == [publisher["email"]]
    # Disabling the partner ends the session.
    client.patch(f"/users/{publisher['id']}", headers=admin, json={"active": False})
    assert client.get("/me", headers=as_pub).status_code == 401
    assert client.post(route, headers=admin).status_code == 409


def test_agency_team_members_sign_in_to_the_agency_account(client):
    from test_api import sign_in

    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    agency, agency_headers = partner(client, admin, "affiliate", "agency")
    member_body = {"email": "Riya@Agency.example", "password": "team-password-1234", "name": "Riya"}
    assert client.post("/team", headers=admin, json=member_body).status_code == 403
    assert (
        client.post(
            "/team", headers=agency_headers, json={**member_body, "email": publisher["email"]}
        ).status_code
        == 409
    )
    added = client.post("/team", headers=agency_headers, json=member_body)
    assert added.status_code == 201, added.text
    member = added.json()
    assert member["email"] == "riya@agency.example" and "password" not in added.text
    assert client.post("/team", headers=agency_headers, json=member_body).status_code == 409

    login = client.post(
        "/auth/login", json={"email": "riya@agency.example", "password": "team-password-1234"}
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["id"] == agency["id"]
    assert login.json()["user"]["team_member"] == {"email": "riya@agency.example", "name": "Riya"}
    riya = {"Authorization": "Bearer " + login.json()["access_token"]}
    assert client.get("/me", headers=riya).json()["id"] == agency["id"]
    assert client.get("/users", headers=riya).status_code == 200
    assert client.get("/team", headers=riya).status_code == 403
    assert any("[by team member riya@agency.example]" in d for d in _audit_details("auth.login"))
    wrong = client.post(
        "/auth/login", json={"email": "riya@agency.example", "password": "wrong-password-1234"}
    )
    assert wrong.status_code == 401

    # A new password ends existing sessions; disabling blocks sign-in.
    reset = client.put(
        f"/team/{member['id']}/password",
        headers=agency_headers,
        json={"password": "new-password-12345"},
    )
    assert reset.status_code == 200
    assert client.get("/me", headers=riya).status_code == 401
    riya = sign_in(client, None, "riya@agency.example", "new-password-12345")
    client.patch(f"/team/{member['id']}", headers=agency_headers, json={"active": False})
    assert client.get("/me", headers=riya).status_code == 401
    disabled = client.post(
        "/auth/login", json={"email": "riya@agency.example", "password": "new-password-12345"}
    )
    assert disabled.status_code == 401
    listed = client.get("/team", headers=agency_headers).json()
    assert [(m["email"], m["active"]) for m in listed] == [("riya@agency.example", False)]
    _, other_agency = partner(client, admin, "affiliate", "other-agency")
    assert (
        client.patch(
            f"/team/{member['id']}", headers=other_agency, json={"active": True}
        ).status_code
        == 404
    )
    from test_api import signup_body

    taken = client.post(
        "/auth/register", json={**signup_body("Copycat"), "email": "riya@agency.example"}
    )
    assert taken.status_code == 409


def test_account_managers_are_assigned_and_shown_to_partners(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    manager, manager_headers = partner(client, admin, "staff", "manager")
    route = f"/users/{publisher['id']}/manager"
    assert client.put(route, headers=adv, json={"manager_id": manager["id"]}).status_code == 403
    assert (
        client.put(route, headers=admin, json={"manager_id": advertiser["id"]}).status_code == 422
    )
    assert client.put(route, headers=admin, json={"manager_id": manager["id"]}).status_code == 200
    assert client.get("/account-manager", headers=pub).json() == {
        "name": "",
        "email": manager["email"],
    }
    assert client.get("/account-manager", headers=adv).json() is None
    mine = client.get("/managed-partners", headers=manager_headers).json()
    assert [u["id"] for u in mine] == [publisher["id"]] and mine[0]["manager_id"] == manager["id"]
    assert client.get("/managed-partners", headers=pub).status_code == 403
    client.put(route, headers=admin, json={"manager_id": None})
    assert client.get("/account-manager", headers=pub).json() is None


def test_postback_token_can_be_a_parameter_and_app_events_map_to_stages(client):
    from app.core.database import SessionLocal
    from app.persistence.models import Conversion

    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    goal(client, adv, campaign, code="purchase", name="First purchase", payout="50")
    token = client.get("/postback-settings", headers=admin).json()["url"].rsplit("/pb/", 1)[1]
    assert client.get("/pb", params={"click_id": click, "txn_id": "APP1"}).status_code == 401
    wrong = client.get("/pb", params={"token": "x" * 32, "click_id": click, "txn_id": "APP1"})
    assert wrong.status_code == 401
    # An attribution platform reports the install, then an in-app event by its own name.
    install = client.get(
        "/pb", params={"token": token, "click_id": click, "txn_id": "DEVICE1", "event": "install"}
    )
    assert install.status_code == 201, install.text
    purchase = client.post(
        "/pb",
        json={
            "security_token": token,
            "click_id": click,
            "txn_id": "DEVICE1",
            "event": "af_purchase",
        },
    )
    assert purchase.status_code == 201, purchase.text
    with SessionLocal() as db:
        stored = sorted(
            (c.event_key, c.transaction_id)
            for c in db.query(Conversion).filter_by(offer_id=campaign["id"])
        )
    assert stored == [("initial", "DEVICE1"), ("purchase", "DEVICE1")]
    # An unknown app event is still refused rather than guessed.
    unknown = client.get(
        "/pb",
        params={"token": token, "click_id": click, "txn_id": "DEVICE1", "event": "af_level_up"},
    )
    assert unknown.status_code in {404, 422}, unknown.text


def test_links_can_be_listed_for_one_campaign(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    other = client.post(
        "/campaigns",
        headers=adv,
        json={
            "name": "Other",
            "destination_url": "https://brand.example/o",
            "revenue": "1",
            "payout": "1",
        },
    ).json()
    client.post(
        f"/campaigns/{other['id']}/assign",
        headers=admin,
        json={"publisher_id": publisher["id"], "status": "approved"},
    )
    assert (
        client.post(
            "/links", headers=admin, json={"offer_id": other["id"], "affiliate_id": publisher["id"]}
        ).status_code
        == 201
    )
    assert len(client.get("/links", headers=admin).json()) == 2
    only = client.get("/links", headers=admin, params={"offer_id": campaign["id"]}).json()
    assert [r["id"] for r in only] == [link["id"]]
