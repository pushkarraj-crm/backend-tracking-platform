"""HTTPS delivery with public-address validation and pinned DNS to prevent SSRF/rebinding."""

import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit
from fastapi import HTTPException
from .config import get_settings


def validate_destination(url):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise HTTPException(422, "Webhook URL requires HTTPS on port 443 without credentials")
    allowed = get_settings().outbound_allowed_hosts
    if parts.hostname not in allowed:
        raise HTTPException(422, "Webhook host is not enabled for outbound delivery")
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError:
        raise HTTPException(422, "Webhook host could not be resolved")
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise HTTPException(422, "Webhook host must resolve only to public addresses")
    return parts, sorted(addresses)[0]


class WebhookResponseError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__("Callback returned an unsuccessful HTTP status")


def deliver_webhook(url, secret, event_id, event, method="POST"):
    if method not in {"GET", "POST"}:
        raise ValueError("Unsupported callback method")
    from .callbacks import render_url

    url = render_url(url, event)
    event_type = "integration.test" if event.get("is_test") else "conversion.updated"
    if method == "GET":
        parts = urlsplit(url)
        query = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in {"relay_event_id", "relay_event_type", "relay_version"}
        ]
        query.extend(
            [
                ("relay_event_id", event_id),
                ("relay_event_type", event_type),
                ("relay_version", str(event.get("version", 0))),
            ]
        )
        url = urlunsplit(parts._replace(query=urlencode(query)))
    parts, address = validate_destination(url)
    body = json.dumps(
        {"id": event_id, "type": event_type, "data": event},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    timestamp = str(int(time.time()))
    signing_payload = b"GET\n" + path.encode() if method == "GET" else body
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + signing_payload, hashlib.sha256
    ).hexdigest()
    connection = http.client.HTTPSConnection(parts.hostname, 443, timeout=10)
    # Resolve once and connect to that exact public address while validating TLS for the original hostname.
    connection.sock = ssl.create_default_context().wrap_socket(
        socket.create_connection((address, 443), timeout=10), server_hostname=parts.hostname
    )
    try:
        connection.request(
            method,
            path,
            body=None if method == "GET" else body,
            headers={
                "Content-Type": "application/json",
                "X-Event-ID": event_id,
                "X-Webhook-Timestamp": timestamp,
                "X-Webhook-Signature": signature,
                "X-Webhook-Signature-Version": "2" if method == "GET" else "1",
                "X-Event-Type": event_type,
            },
        )
        response = connection.getresponse()
        response.read(4096)
        if not 200 <= response.status < 300:
            raise WebhookResponseError(response.status)
        return response.status
    finally:
        connection.close()
