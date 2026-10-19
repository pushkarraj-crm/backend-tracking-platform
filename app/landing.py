"""Advertiser-owned, crawlable landing pages with escaped content and explicit CTAs."""

from html import escape
from urllib.parse import urlencode, urlsplit
from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import HTMLResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .models import LandingPage, TrackingDomain, Offer, TrackingLink, Workspace
from .schemas import StrictModel, ActiveUpdate
from .security import DB, Actor
from .domains import enabled
from .campaigns import manage_campaign
from .services import audit, entitlement

router = APIRouter(tags=["Advertiser landing pages"])


class LandingBody(StrictModel):
    offer_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    domain_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)
    button_text: str = Field(default="Continue", min_length=1, max_length=80)


def output(page, domain):
    return {
        **{
            k: getattr(page, k)
            for k in (
                "id",
                "offer_id",
                "domain_id",
                "slug",
                "title",
                "body",
                "button_text",
                "active",
            )
        },
        "url": f"https://{domain.hostname}/l/{page.slug}",
    }


@router.post("/landing-pages", status_code=201)
def create_page(body: LandingBody, db: DB, user: Actor):
    entitlement(db.get(Workspace, user.workspace_id))
    campaign = manage_campaign(db, user, body.offer_id)
    domain = db.scalar(
        select(TrackingDomain).where(
            TrackingDomain.id == body.domain_id, TrackingDomain.workspace_id == user.workspace_id
        )
    )
    if (
        not enabled(domain)
        or domain.purpose not in {"landing", "both"}
        or not campaign.advertiser_id
        or domain.advertiser_id != campaign.advertiser_id
    ):
        raise HTTPException(
            422, "Choose a verified landing domain belonging to this campaign's advertiser"
        )
    if urlsplit(campaign.destination_url).scheme != "https":
        raise HTTPException(422, "Landing-page destination must use HTTPS")
    page = LandingPage(workspace_id=user.workspace_id, **body.model_dump())
    db.add(page)
    try:
        db.flush()
        audit(db, user, "landing.created", page.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This domain already uses that page path") from None
    return output(page, domain)


@router.get("/landing-pages")
def pages(db: DB, user: Actor, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    query = (
        select(LandingPage, TrackingDomain)
        .join(TrackingDomain, TrackingDomain.id == LandingPage.domain_id)
        .where(LandingPage.workspace_id == user.workspace_id)
    )
    if user.role == "advertiser":
        query = query.where(TrackingDomain.advertiser_id == user.id)
    elif user.role not in {"admin", "staff"}:
        raise HTTPException(403, "Advertiser access required")
    return [
        output(p, d)
        for p, d in db.execute(query.order_by(LandingPage.id).offset(offset).limit(limit))
    ]


@router.patch("/landing-pages/{page_id}")
def page_status(page_id: str, body: ActiveUpdate, db: DB, user: Actor):
    page = db.scalar(
        select(LandingPage)
        .where(LandingPage.id == page_id, LandingPage.workspace_id == user.workspace_id)
        .with_for_update()
    )
    if not page:
        raise HTTPException(404, "Page not found")
    manage_campaign(db, user, page.offer_id)
    page.active = body.active
    audit(db, user, "landing.active", page.id, str(body.active))
    db.commit()
    return output(page, db.get(TrackingDomain, page.domain_id))


@router.get("/l/{slug}", response_class=HTMLResponse, include_in_schema=False)
def render_page(
    slug: str, request: Request, db: DB, link: str | None = Query(None, pattern=r"^[a-f0-9]{32}$")
):
    row = db.execute(
        select(LandingPage, TrackingDomain, Offer)
        .join(TrackingDomain, TrackingDomain.id == LandingPage.domain_id)
        .join(Offer, Offer.id == LandingPage.offer_id)
        .where(TrackingDomain.hostname == request.url.hostname, LandingPage.slug == slug)
    ).first()
    if not row:
        raise HTTPException(404, "Landing page not found")
    page, domain, campaign = row
    if (
        not enabled(domain)
        or domain.purpose not in {"landing", "both"}
        or not page.active
        or not campaign.active
    ):
        raise HTTPException(404, "Landing page unavailable")
    entitlement(db.get(Workspace, page.workspace_id))
    target = campaign.destination_url
    if link:
        record = db.get(TrackingLink, link)
        if (
            not record
            or record.offer_id != page.offer_id
            or record.workspace_id != page.workspace_id
        ):
            raise HTTPException(404, "Campaign link not found")
        # A landing-only hostname cannot serve tracking. Use the canonical API host.
        from .domains import link_base
        from types import SimpleNamespace

        target = link_base(db, SimpleNamespace(workspace_id=page.workspace_id), None) + "/t/" + link
        params = {
            k: v for k, v in request.query_params.items() if k in AD_PARAMETERS and len(v) <= 256
        }
        if params:
            target += "?" + urlencode(params)
    title, body, button = escape(page.title), escape(page.body), escape(page.button_text)
    return HTMLResponse(
        f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title><body><main><h1>{title}</h1><p>{body.replace(chr(10), "<br>")}</p><a href="{escape(target, quote=True)}" rel="sponsored nofollow">{button}</a></main></body></html>',
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            "X-Robots-Tag": "index, follow",
        },
    )


AD_PARAMETERS = {
    "gclid",
    "gbraid",
    "wbraid",
    "fbclid",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "sub_id",
}
