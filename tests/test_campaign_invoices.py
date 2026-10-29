"""Network-scoped campaigns, S2S attribution and direct payment lifecycle."""

from urllib.parse import parse_qs, urlsplit

import pytest
from test_api import client as api_client  # noqa: F401
from test_api import register, sign_in


@pytest.fixture
def client(api_client):  # noqa: F811
    return api_client


def partner(client, admin, role, name):
    email = f"{name}@example.com"
    response = client.post(
        "/users",
        headers=admin,
        json={"email": email, "password": "affiliate-password-123", "role": role},
    )
    assert response.status_code == 201, response.text
    return response.json(), sign_in(client, None, email)


def campaign_flow(client, model="CPL"):
    admin, _ = register(client)
    advertiser, adv = partner(client, admin, "advertiser", "advertiser")
    publisher, pub = partner(client, admin, "publisher", "publisher")
    body = {
        "name": "New campaign",
        "destination_url": "https://brand.example/offer?keep=yes",
        "revenue": "4.25",
        "payout": "4.25",
        "campaign_model": model,
    }
    if model == "CPS":
        body["sale_percentage"] = "12.5"
    response = client.post("/campaigns", headers=adv, json=body)
    assert response.status_code == 201, response.text
    campaign = response.json()
    assert client.post(f"/campaigns/{campaign['id']}/link", headers=pub).status_code == 403
    response = client.post(
        f"/campaigns/{campaign['id']}/apply",
        headers=pub,
        json={"message": "Approved traffic sources"},
    )
    assert response.status_code == 201, response.text
    application = response.json()
    assert (
        client.patch(
            f"/campaign-applications/{application['id']}", headers=adv, json={"status": "approved"}
        ).status_code
        == 200
    )
    response = client.post(f"/campaigns/{campaign['id']}/link", headers=pub)
    assert response.status_code == 201, response.text
    link = response.json()
    click = client.get(
        "/t/" + link["id"] + "?gclid=google123&fbclid=meta123&redirect=https://evil.example",
        follow_redirects=False,
    )
    assert click.status_code == 302, click.text
    params = parse_qs(urlsplit(click.headers["location"]).query)
    assert (
        params["gclid"] == ["google123"]
        and params["fbclid"] == ["meta123"]
        and params["keep"] == ["yes"]
    )
    assert "redirect" not in params
    return admin, adv, pub, advertiser, publisher, campaign, link, params["click_id"][0]


@pytest.mark.parametrize("model,amount", [("CPL", "4.2500"), ("CPS", "12.5000")])
def test_s2s_campaign_to_publisher_confirmed_invoice(client, model, amount):
    admin, adv, pub, advertiser, publisher, campaign, link, click_id = campaign_flow(client, model)
    path = "/postbacks/" + campaign["id"]
    body = {
        "click_id": click_id,
        "transaction_id": "order-001",
        "event_type": "lead" if model == "CPL" else "sale",
    }
    if model == "CPS":
        body.update(sale_amount="100", sale_currency="USD")
    assert client.post(path, json=body, headers={"X-Postback-Key": "incorrect"}).status_code == 401
    headers = {"X-Postback-Key": campaign["postback_key"]}
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 201, response.text
    conversion = response.json()
    assert client.post(path, json=body, headers=headers).status_code == 200
    if model == "CPS":
        assert (
            client.post(path, json={**body, "sale_amount": "200"}, headers=headers).status_code
            == 409
        )
        assert (
            client.post(path, json={**body, "sale_currency": "INR"}, headers=headers).status_code
            == 422
        )
    assert (
        client.patch(
            "/conversions/" + conversion["id"], headers=adv, json={"status": "approved"}
        ).status_code
        == 200
    )
    eligible = client.get("/publisher-invoices/eligible-conversions", headers=pub)
    assert eligible.status_code == 200, eligible.text
    assert eligible.json()[0]["amount"] == amount
    request = {"number": "INV-001", "conversion_ids": [conversion["id"]]}
    response = client.post("/publisher-invoices", headers=pub, json=request)
    assert response.status_code == 201, response.text
    invoice = response.json()
    assert invoice["amount"] == amount
    # Duplicate submissions return the same immutable invoice, never another payable.
    assert (
        client.post("/publisher-invoices", headers=pub, json=request).json()["id"] == invoice["id"]
    )
    assert (
        client.post(
            "/publisher-invoices", headers=pub, json={**request, "number": "INV-002"}
        ).status_code
        == 409
    )
    assert (
        client.patch(
            "/conversions/" + conversion["id"], headers=adv, json={"status": "rejected"}
        ).status_code
        == 409
    )
    endpoint = "/publisher-invoices/" + invoice["id"] + "/actions"
    assert client.post(endpoint, headers=admin, json={"action": "approve"}).status_code == 403
    assert client.post(endpoint, headers=pub, json={"action": "approve"}).status_code == 403
    assert (
        client.post(
            endpoint,
            headers=adv,
            json={"action": "report_payment", "payment_reference": "too-early"},
        ).status_code
        == 409
    )
    assert (
        client.post(endpoint, headers=adv, json={"action": "approve"}).json()["status"]
        == "approved"
    )
    assert (
        client.post(
            endpoint,
            headers=adv,
            json={"action": "report_payment", "payment_reference": "BANK-001"},
        ).json()["status"]
        == "payment_reported"
    )
    assert client.post(endpoint, headers=adv, json={"action": "confirm_receipt"}).status_code == 403
    assert (
        client.post(
            endpoint, headers=pub, json={"action": "dispute", "note": "Payment has not arrived"}
        ).json()["status"]
        == "disputed"
    )
    assert (
        client.post(
            endpoint,
            headers=adv,
            json={"action": "report_payment", "payment_reference": "BANK-002"},
        ).json()["status"]
        == "payment_reported"
    )
    assert (
        client.post(endpoint, headers=pub, json={"action": "confirm_receipt"}).json()["status"]
        == "paid"
    )
    detail = client.get("/publisher-invoices/" + invoice["id"], headers=pub).json()
    assert len(detail["events"]) == 6 and detail["confirmed_at"]
    assert client.get("/payouts/balances", headers=admin).json() == []
    reports = [e for e in detail["events"] if e["action"] == "report_payment"]
    assert [e["evidence"]["payment_reference"] for e in reports] == ["BANK-001", "BANK-002"]
    other, other_adv = partner(client, admin, "advertiser", "other-advertiser")
    assert client.get("/publisher-invoices/" + invoice["id"], headers=other_adv).status_code == 404
    assert client.get("/campaigns", headers=other_adv).json() == []
    assert client.get("/campaign-applications", headers=other_adv).json() == []


def test_reject_invoice_releases_conversion_and_revoked_application_blocks_traffic(client):
    admin, adv, pub, advertiser, publisher, campaign, link, click_id = campaign_flow(client)
    conversion = client.post(
        "/postbacks/" + campaign["id"],
        headers={"X-Postback-Key": campaign["postback_key"]},
        json={"click_id": click_id, "transaction_id": "lead-1"},
    ).json()
    client.patch("/conversions/" + conversion["id"], headers=adv, json={"status": "approved"})
    invoice = client.post(
        "/publisher-invoices",
        headers=pub,
        json={"number": "ONE", "conversion_ids": [conversion["id"]]},
    ).json()
    assert (
        client.post(
            "/publisher-invoices/" + invoice["id"] + "/actions",
            headers=adv,
            json={"action": "reject", "note": "Incorrect invoice number"},
        ).json()["status"]
        == "rejected"
    )
    assert (
        client.post(
            "/publisher-invoices",
            headers=pub,
            json={"number": "TWO", "conversion_ids": [conversion["id"]]},
        ).status_code
        == 201
    )
    application = client.get("/campaign-applications", headers=adv).json()[0]
    client.patch(
        "/campaign-applications/" + application["id"], headers=adv, json={"status": "rejected"}
    )
    assert client.get("/t/" + link["id"], follow_redirects=False).status_code == 403


def test_retired_funding_never_calls_provider(client, monkeypatch):
    from sqlalchemy import select

    from app import maintenance
    from app.core.database import SessionLocal
    from app.features.billing import automation as automated_payouts
    from app.persistence.models import OutboxEvent, Payout, PayoutRequest

    admin, _ = register(client)
    for method, path in [
        ("PUT", "provider"),
        ("POST", "requests"),
        ("PUT", "beneficiaries"),
        ("POST", "webhook"),
        ("GET", "requests"),
    ]:
        assert (
            client.request(method, "/automated-payouts/" + path, headers=admin, json={}).status_code
            == 410
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("A provider must never be called")

    monkeypatch.setattr(automated_payouts, "provider_call", forbidden)
    with SessionLocal() as db:
        assert automated_payouts.schedule(db) == 0
        with pytest.raises(RuntimeError, match="disabled"):
            automated_payouts.process(db, "old-job", "network")
        assert (
            list(db.scalars(select(Payout))) == [] and list(db.scalars(select(PayoutRequest))) == []
        )
    monkeypatch.setattr(maintenance, "revalidate_domains", lambda: None)
    maintenance.run_once()
    with SessionLocal() as db:
        assert not list(db.scalars(select(OutboxEvent).where(OutboxEvent.kind == "payout")))


def test_advertiser_domains_and_escaped_crawlable_landing(client, monkeypatch):
    from app.core.config import get_settings
    from app.features.domains import router as domains

    monkeypatch.setattr(get_settings(), "tracking_cname", "gateway.example.com")
    monkeypatch.setattr(domains, "verify_dns", lambda domain: True)
    admin, adv, pub, advertiser, publisher, campaign, link, click = campaign_flow(client)
    response = client.post(
        "/tracking-domains",
        headers=adv,
        json={"hostname": "offers.brand.example", "purpose": "both"},
    )
    assert response.status_code == 201, response.text
    domain = response.json()
    assert (
        client.post("/tracking-domains/" + domain["id"] + "/verify", headers=adv).status_code == 200
    )
    other, other_adv = partner(client, admin, "advertiser", "other-owner")
    assert client.get("/tracking-domains", headers=other_adv).json() == []
    assert (
        client.post("/tracking-domains/" + domain["id"] + "/verify", headers=other_adv).status_code
        == 403
    )
    body = {
        "offer_id": campaign["id"],
        "domain_id": domain["id"],
        "slug": "summer",
        "title": "Summer <offer>",
        "body": "<script>alert(1)</script>",
        "button_text": "View offer",
    }
    response = client.post("/landing-pages", headers=adv, json=body)
    assert response.status_code == 201, response.text
    page = client.get(
        "/l/summer?link=" + link["id"],
        headers={"host": "offers.brand.example", "user-agent": "Googlebot"},
    )
    assert page.status_code == 200, page.text
    assert "&lt;script&gt;" in page.text and "<script>" not in page.text
    assert "/t/" + link["id"] in page.text
    assert client.get("/l/summer", headers={"host": "other.brand.example"}).status_code == 400
    assert client.get("/users", headers={"host": "offers.brand.example"}).status_code == 400
    assert client.post("/landing-pages", headers=other_adv, json=body).status_code == 404
    selected = client.post(
        "/campaigns/" + campaign["id"] + "/link?domain_id=" + domain["id"], headers=pub
    )
    assert selected.status_code == 201 and selected.json()["url"].startswith(
        "https://offers.brand.example/"
    )
    assert selected.json()["final_urls"] == [
        f"https://offers.brand.example/l/summer?link={link['id']}"
    ]


def test_concurrent_invoices_reserve_a_conversion_once(client):
    from concurrent.futures import ThreadPoolExecutor

    admin, adv, pub, advertiser, publisher, campaign, link, click_id = campaign_flow(client)
    conversion = client.post(
        "/postbacks/" + campaign["id"],
        headers={"X-Postback-Key": campaign["postback_key"]},
        json={"click_id": click_id, "transaction_id": "concurrent-invoice"},
    ).json()
    client.patch("/conversions/" + conversion["id"], headers=adv, json={"status": "approved"})

    def submit(number):
        return client.post(
            "/publisher-invoices",
            headers=pub,
            json={"number": number, "conversion_ids": [conversion["id"]]},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["INV-A", "INV-B"]))
    assert sorted(results) == [201, 409]
    assert len(client.get("/publisher-invoices", headers=pub).json()) == 1


def test_signed_publisher_s2s_event_contains_commission_and_is_durable(client, monkeypatch):
    import hashlib
    import hmac
    import json
    from types import SimpleNamespace

    from sqlalchemy import select

    from app import worker
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.features.callbacks import delivery as delivery
    from app.persistence.models import OutboxEvent

    admin, adv, pub, advertiser, publisher, campaign, link, click_id = campaign_flow(client)
    monkeypatch.setattr(get_settings(), "outbound_allowed_hosts", ["publisher.example"])
    monkeypatch.setattr(
        delivery,
        "socket",
        SimpleNamespace(
            SOCK_STREAM=delivery.socket.SOCK_STREAM,
            getaddrinfo=lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
        ),
    )
    configured = client.put(
        "/publisher-postback", headers=pub, json={"url": "https://publisher.example/events"}
    )
    assert configured.status_code == 200, configured.text
    secret = configured.json()["secret"]
    assert "secret" not in client.get("/publisher-postback", headers=pub).json()
    conversion = client.post(
        "/postbacks/" + campaign["id"],
        headers={"X-Postback-Key": campaign["postback_key"]},
        json={"click_id": click_id, "transaction_id": "callback-test"},
    ).json()
    client.patch("/conversions/" + conversion["id"], headers=adv, json={"status": "approved"})
    captured = {}

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, body, headers):
            captured.update(method=method, path=path, body=body, headers=headers)

        def getresponse(self):
            class Response:
                status = 200

                def read(self, n):
                    return b""

            return Response()

        def close(self):
            pass

    class Context:
        def wrap_socket(self, *args, **kwargs):
            return object()

    monkeypatch.setattr(
        delivery, "http", SimpleNamespace(client=SimpleNamespace(HTTPSConnection=Connection))
    )
    monkeypatch.setattr(
        delivery.socket, "create_connection", lambda *a, **k: object(), raising=False
    )
    monkeypatch.setattr(delivery, "ssl", SimpleNamespace(create_default_context=lambda: Context()))
    with SessionLocal() as db:
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.kind == "webhook"))
        assert event and event.status == "pending"
        worker.dispatch(event.kind, event.payload, event.id, event.workspace_id)
        event_id = event.id
    assert captured["headers"]["X-Event-ID"] == event_id
    signature = hmac.new(
        secret.encode(),
        captured["headers"]["X-Webhook-Timestamp"].encode() + b"." + captured["body"],
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(signature, captured["headers"]["X-Webhook-Signature"])
    data = json.loads(captured["body"])["data"]
    assert (
        data["conversion_id"] == conversion["id"]
        and data["status"] == "approved"
        and data["payout"] == "4.2500"
        and data["version"] == 1
    )
