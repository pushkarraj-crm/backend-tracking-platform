from fastapi import HTTPException
from sqlalchemy import select

from app.persistence.models import Offer


def manage_campaign(db, user, campaign_id, lock=False):
    campaign = db.scalar(
        select(Offer)
        .where(Offer.id == campaign_id, Offer.workspace_id == user.workspace_id)
        .with_for_update()
        if lock
        else select(Offer).where(Offer.id == campaign_id, Offer.workspace_id == user.workspace_id)
    )
    if (
        not campaign
        or campaign.campaign_model not in {"CPL", "CPS"}
        or (user.role == "advertiser" and campaign.advertiser_id != user.id)
    ):
        raise HTTPException(404, "Campaign not found")
    if user.role not in {"admin", "staff", "advertiser"}:
        raise HTTPException(403, "Campaign management access required")
    return campaign
