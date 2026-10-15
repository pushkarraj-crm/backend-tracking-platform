"""Durable outbox consumer. Run one or more processes with `python -m app.worker`."""

import json
import logging
import random
import signal
import smtplib
import ssl
import time
from datetime import timedelta
from email.message import EmailMessage
from sqlalchemy import select, or_, and_
from .config import get_settings
from .db import SessionLocal
from .models import OutboxEvent, WebhookEndpoint, Workspace, now, identifier
from .services import decrypt, encrypt
from .delivery import deliver_webhook
from .billing import reconcile_subscription
from .automated_payouts import process as process_payout

settings = get_settings()
logger = logging.getLogger("affiliate.worker")
stopping = False


def claim():
    with SessionLocal.begin() as db:
        event = db.scalar(
            select(OutboxEvent)
            .where(
                or_(
                    and_(OutboxEvent.status == "pending", OutboxEvent.available_at <= now()),
                    and_(OutboxEvent.status == "processing", OutboxEvent.lease_until < now()),
                )
            )
            .order_by(OutboxEvent.available_at, OutboxEvent.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not event:
            return None
        event.status = "processing"
        event.lease_token = identifier()
        event.lease_until = now() + timedelta(seconds=60)
        event.attempts += 1
        db.flush()
        return event.id, event.lease_token, event.kind, event.payload, event.workspace_id


def dispatch(kind, encrypted_payload, event_id, workspace_id):
    payload = json.loads(decrypt(encrypted_payload))
    if kind == "email":
        if not settings.smtp_host or not settings.smtp_from:
            raise RuntimeError("SMTP unavailable")
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = payload["to"]
        message["Subject"] = (
            "Verify your email" if payload["purpose"] == "verify" else "Reset your password"
        )
        message.set_content(
            "Use this link within 30 minutes:\n"
            + payload["url"]
            + "\nIf you did not request this, ignore this email."
        )
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
    elif kind == "webhook":
        with SessionLocal() as db:
            endpoint = db.get(WebhookEndpoint, payload["endpoint_id"])
            if not endpoint or endpoint.workspace_id != workspace_id or not endpoint.active:
                return
            deliver_webhook(endpoint.url, decrypt(endpoint.secret), event_id, payload["event"])
    elif kind == "billing":
        with SessionLocal.begin() as db:
            workspace = db.scalar(
                select(Workspace).where(Workspace.id == workspace_id).with_for_update()
            )
            reconcile_subscription(db, workspace, payload["subscription_id"])
    elif kind == "payout":
        with SessionLocal.begin() as db:
            process_payout(db, payload["request_id"], workspace_id)
    else:
        raise RuntimeError("Unknown delivery type")


def run_once():
    claimed = claim()
    if not claimed:
        return False
    event_id, lease, kind, payload, workspace_id = claimed
    error = None
    try:
        dispatch(kind, payload, event_id, workspace_id)
    except Exception as exception:
        error = type(exception).__name__
    with SessionLocal.begin() as db:
        event = db.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                OutboxEvent.lease_token == lease,
                OutboxEvent.status == "processing",
            )
            .with_for_update()
        )
        if not event:
            return True
        event.last_error = error
        event.lease_until = None
        event.lease_token = None
        if error:
            event.status = "failed" if event.attempts >= 8 else "pending"
            event.available_at = now() + timedelta(
                seconds=min(3600, 2**event.attempts * 5) + random.randint(0, 10)
            )
        else:
            event.status = "delivered"
            event.payload = encrypt("{}")
    logger.info("delivery event=%s kind=%s result=%s", event_id, kind, error or "delivered")
    return True


def stop(signum, frame):
    global stopping
    stopping = True


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        try:
            if not run_once():
                time.sleep(settings.worker_poll_seconds)
        except Exception as error:
            logger.error("worker iteration failed: %s", type(error).__name__)
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
