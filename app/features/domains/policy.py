"""Domain validity shared by request middleware and domain endpoints."""

from datetime import datetime, timezone

from app.core.services import utc


def enabled(domain):
    return bool(
        domain
        and domain.active
        and domain.verification_expires_at
        and utc(domain.verification_expires_at) > datetime.now(timezone.utc)
    )
