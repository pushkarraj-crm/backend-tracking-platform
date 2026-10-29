"""Authenticate visitor identity forwarded by the frontend's server-side proxy.

Direct requests keep the server's connection identity. Forwarded-for headers alone
never grant trust. This signature authenticates routing metadata, not API access.
"""

import hashlib
import hmac
import ipaddress
import time

IDENTITY_HEADERS = {b"x-relay-client-ip", b"x-relay-proxy-time", b"x-relay-proxy-signature"}


def restore_client(scope, secret):
    items = [(key.lower(), value) for key, value in scope.get("headers", [])]
    supplied = [key for key, _ in items if key in IDENTITY_HEADERS]
    if not supplied:
        return scope
    if not secret or len(supplied) != 3 or set(supplied) != IDENTITY_HEADERS:
        raise ValueError("Incomplete or unconfigured proxy identity")
    try:
        headers = dict(items)
        ip = headers[b"x-relay-client-ip"].decode("ascii")
        timestamp = headers[b"x-relay-proxy-time"].decode("ascii")
        signature = headers[b"x-relay-proxy-signature"].decode("ascii")
        if len(ip) > 45 or len(timestamp) > 12 or abs(time.time() - int(timestamp)) > 60:
            raise ValueError("Expired proxy identity")
        address = ipaddress.ip_address(ip)
        target = scope.get("raw_path", scope["path"].encode()).decode("ascii")
        if scope.get("query_string"):
            target += "?" + scope["query_string"].decode("ascii")
        company = headers.get(b"x-company-slug", b"").decode("ascii")
        message = "\n".join(["relay-ip-v1", timestamp, ip, scope["method"], target, company])
        expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid proxy signature")
    except (KeyError, UnicodeError, ValueError) as error:
        raise ValueError("Invalid proxy identity") from error
    # Normalize mapped IPv4 so its rate-limit key matches direct IPv4 traffic.
    address = getattr(address, "ipv4_mapped", None) or address
    return {**scope, "client": (str(address), 0)}
