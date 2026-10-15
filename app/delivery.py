"""HTTPS delivery with public-address validation and pinned DNS to prevent SSRF/rebinding."""

import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import urlsplit
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


def deliver_webhook(url, secret, event_id, event):
    parts, address = validate_destination(url)
    body = json.dumps(
        {"id": event_id, "type": "conversion.updated", "data": event},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    connection = http.client.HTTPSConnection(parts.hostname, 443, timeout=10)
    # Resolve once and connect to that exact public address while validating TLS for the original hostname.
    connection.sock = ssl.create_default_context().wrap_socket(
        socket.create_connection((address, 443), timeout=10), server_hostname=parts.hostname
    )
    try:
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Event-ID": event_id,
                "X-Webhook-Timestamp": timestamp,
                "X-Webhook-Signature": signature,
            },
        )
        response = connection.getresponse()
        response.read(4096)
        if not 200 <= response.status < 300:
            raise RuntimeError("Webhook rejected")
    finally:
        connection.close()
