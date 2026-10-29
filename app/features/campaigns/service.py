import secrets

from fastapi import HTTPException
from sqlalchemy import select

from app.core.security import key_hash, passwords
from app.core.services import audit, quota
from app.features.campaigns.presentation import output_offer
from app.permissions.resources import owned
from app.persistence.models import (
    Offer,
    User,
    Workspace,
)


def campaign_output(campaign, user):
    result = output_offer(campaign)
    from app.features.campaigns.policy import public_settings

    result["settings"] = public_settings(campaign, user)
    result.update(
        campaign_model=campaign.campaign_model,
        sale_percentage=str(campaign.sale_percentage)
        if campaign.sale_percentage is not None
        else None,
        description=campaign.description,
    )
    if user.role in {"publisher", "affiliate"}:
        for name in ("revenue", "destination_url", "postback_url"):
            result.pop(name, None)
    return result


def house_advertiser(db, workspace):
    """A brand account is its own advertiser: one login-less advertiser record owns its campaigns."""
    email = f"brand@{workspace.id}.house.invalid"
    advertiser = db.scalar(
        select(User).where(User.workspace_id == workspace.id, User.email == email)
    )
    if not advertiser:
        advertiser = User(
            workspace_id=workspace.id,
            email=email,
            # Nobody knows this password, so the record cannot sign in.
            password_hash=passwords.hash(secrets.token_urlsafe(32)),
            role="advertiser",
            full_name=workspace.name,
            email_verified=True,
        )
        db.add(advertiser)
        db.flush()
    return advertiser


def create_campaign_record(body, db, user):
    if user.role not in {"advertiser", "admin", "staff"}:
        raise HTTPException(403, "Advertiser access required")
    workspace = db.get(Workspace, user.workspace_id)
    if workspace.account_type == "brand" and user.role != "advertiser":
        body = body.model_copy(update={"advertiser_id": house_advertiser(db, workspace).id})
    if user.role != "advertiser" and not body.advertiser_id:
        raise HTTPException(422, "Select the advertiser responsible for this campaign")
    advertiser_id = user.id if user.role == "advertiser" else body.advertiser_id
    if user.role == "advertiser" and body.advertiser_id not in {None, user.id}:
        raise HTTPException(403, "Cannot create campaigns for another advertiser")
    advertiser = owned(db, User, advertiser_id, user)
    if advertiser.role != "advertiser" or not advertiser.active:
        raise HTTPException(422, "Select an active advertiser")
    quota(db, user, Offer, "offers")
    key = secrets.token_urlsafe(32)
    campaign = Offer(
        workspace_id=user.workspace_id,
        **body.model_dump(
            exclude={
                "destination_url",
                "advertiser_id",
                "settings",
                "tracking_rules",
                "active",
                "events",
            }
        ),
        destination_url=str(body.destination_url),
        advertiser_id=advertiser_id,
        postback_key_hash=key_hash(key),
        active=body.active,
        catalog_settings=body.settings.model_dump(mode="json"),
        tracking_rules=body.tracking_rules.model_dump(mode="json"),
    )
    db.add(campaign)
    db.flush()
    from app.persistence.network_models import CampaignEvent

    for event in body.events:
        db.add(
            CampaignEvent(
                workspace_id=user.workspace_id, offer_id=campaign.id, **event.model_dump()
            )
        )
    audit(db, user, "campaign.created", campaign.id)
    return campaign, key


def application_output(row):
    return {
        k: getattr(row, k)
        for k in (
            "id",
            "offer_id",
            "publisher_id",
            "status",
            "message",
            "review_note",
            "created_at",
        )
    }
