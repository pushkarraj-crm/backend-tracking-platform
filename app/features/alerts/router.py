"""Slack and Telegram alerts for caps, postback errors and fraud."""

import json
import re
import urllib.request
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import Field

from app.core.security import DB, Actor
from app.core.services import audit, decrypt, encrypt, enqueue
from app.persistence.models import Workspace
from app.shared.schemas import StrictModel

router = APIRouter(tags=["Alerts"])
EVENTS = ["caps", "postback_errors", "fraud"]
# Alerts only ever go to these two fixed services, so no arbitrary outbound host is reachable.
SLACK_URL = re.compile(r"^https://hooks\.slack\.com/services/[A-Za-z0-9_/-]{20,200}$")
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class AlertSettingsBody(StrictModel):
    # Omit a field to keep it; send "" to remove it.
    slack_webhook_url: str | None = Field(default=None, max_length=300)
    telegram_bot_token: str | None = Field(
        default=None, pattern=r"^(|\d{5,15}:[A-Za-z0-9_-]{30,60})$"
    )
    telegram_chat_id: str | None = Field(
        default=None, pattern=r"^(|-?\d{1,20}|@[A-Za-z0-9_]{5,32})$"
    )
    events: list[Literal["caps", "postback_errors", "fraud"]] = Field(
        default_factory=lambda: list(EVENTS), max_length=3
    )


class TestBody(StrictModel):
    channel: Literal["slack", "telegram"]


def stored(workspace):
    if not workspace.alert_settings:
        return {"events": list(EVENTS)}
    return json.loads(decrypt(workspace.alert_settings))


def channels(settings):
    found = []
    if settings.get("slack_webhook_url"):
        found.append("slack")
    if settings.get("telegram_bot_token") and settings.get("telegram_chat_id"):
        found.append("telegram")
    return found


def send(channel, settings, text):
    """Deliver one message; raises on any failure. Callers never echo the error text."""
    if channel == "slack":
        url, body = settings["slack_webhook_url"], {"text": text}
    else:
        url = TELEGRAM_API.format(token=settings["telegram_bot_token"])
        body = {
            "chat_id": settings["telegram_chat_id"],
            "text": text,
            "disable_web_page_preview": True,
        }
    request = urllib.request.Request(
        url, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - fixed hosts
        if not 200 <= response.status < 300:
            raise RuntimeError("Alert channel returned an unsuccessful status")


def alert(db, workspace_id, event, text):
    """Queue an alert to every configured channel that wants this kind of event."""
    workspace = db.get(Workspace, workspace_id)
    settings = stored(workspace)
    if event not in settings.get("events", []):
        return
    for channel in channels(settings):
        enqueue(
            db,
            workspace_id,
            "alert",
            {"channel": channel, "text": f"Relay · {workspace.name}\n{text}"},
        )


def output(settings):
    return {
        "slack": bool(settings.get("slack_webhook_url")),
        "telegram": bool(settings.get("telegram_bot_token")),
        "telegram_chat_id": settings.get("telegram_chat_id") or "",
        "events": settings.get("events", []),
    }


def admin_only(user):
    if user.role != "admin":
        raise HTTPException(403, "Network admin required")


@router.get("/alert-settings")
def alert_settings(db: DB, user: Actor):
    admin_only(user)
    return output(stored(db.get(Workspace, user.workspace_id)))


@router.put("/alert-settings")
def save_alert_settings(body: AlertSettingsBody, db: DB, user: Actor):
    admin_only(user)
    workspace = db.get(Workspace, user.workspace_id)
    settings = stored(workspace)
    if body.slack_webhook_url and not SLACK_URL.match(body.slack_webhook_url):
        raise HTTPException(
            422, "Use a Slack incoming webhook URL (https://hooks.slack.com/services/…)"
        )
    for name in ("slack_webhook_url", "telegram_bot_token", "telegram_chat_id"):
        if name in body.model_fields_set:
            settings[name] = getattr(body, name) or None
    settings["events"] = body.events
    workspace.alert_settings = encrypt(json.dumps(settings))
    audit(db, user, "alerts.updated", workspace.id, ",".join(channels(settings)) or "none")
    db.commit()
    return output(settings)


@router.post("/alert-settings/test")
def test_alert(body: TestBody, db: DB, user: Actor):
    admin_only(user)
    workspace = db.get(Workspace, user.workspace_id)
    settings = stored(workspace)
    if body.channel not in channels(settings):
        raise HTTPException(409, f"Set up {body.channel.title()} first")
    try:
        send(
            body.channel,
            settings,
            f"Relay · {workspace.name}\nTest alert: this channel is connected.",
        )
    except Exception:
        # Never echo the provider's error: a Telegram URL contains the bot token.
        raise HTTPException(
            502,
            f"{body.channel.title()} did not accept the message. Check the details and try again.",
        ) from None
    return {"sent": True}
