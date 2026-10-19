"""Public impression pixel and consent-gated, redirectless click capture."""

import base64
from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import JSONResponse, Response
from .security import DB
from .models import identifier
from .network_models import Impression
from .tracking_runtime import record_click, resolve_link
from .campaign_policy import safe_url, parameters

router = APIRouter(tags=["Web tracking"])
PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


@router.get("/i/{link_id}", include_in_schema=False)
def impression(
    link_id: str, request: Request, db: DB, sub_id: str | None = Query(None, max_length=100)
):
    from .tracking import check_click
    from .domains import enforce_tracking_host

    link, offer, publisher = resolve_link(db, link_id)
    enforce_tracking_host(db, request, link.workspace_id, offer.advertiser_id)
    metadata = check_click(db, offer, request)
    db.add(
        Impression(
            id=identifier(),
            workspace_id=link.workspace_id,
            link_id=link.id,
            country=metadata["country"],
            device=metadata["device"],
            operating_system=metadata["operating_system"],
            attributes=parameters(request.query_params),
            sub_id=sub_id,
        )
    )
    db.commit()
    return Response(
        PIXEL,
        media_type="image/gif",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )


@router.get("/capture/{link_id}")
def capture(
    link_id: str,
    request: Request,
    db: DB,
    page_url: str = Query(max_length=2048),
    consent: bool = False,
    sub_id: str | None = Query(None, max_length=100),
):
    if not consent:
        raise HTTPException(422, "Click capture requires an explicit consent signal")
    link, offer, publisher = resolve_link(db, link_id)
    try:
        safe_url(page_url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    parts = urlsplit(page_url)
    origin = request.headers.get("origin", "")
    expected = "https://" + parts.netloc
    if origin != expected:
        raise HTTPException(403, "Capture origin does not match the page URL")
    # An advertiser authorizes capture on its configured destination and its verified landing hosts.
    allowed = {urlsplit(offer.destination_url).hostname}
    from .models import TrackingDomain
    from .domains import enabled
    from sqlalchemy import select

    allowed.update(
        d.hostname
        for d in db.scalars(
            select(TrackingDomain).where(
                TrackingDomain.workspace_id == offer.workspace_id,
                TrackingDomain.advertiser_id == offer.advertiser_id,
                TrackingDomain.purpose.in_(["landing", "both"]),
            )
        )
        if enabled(d)
    )
    if parts.hostname not in allowed:
        raise HTTPException(403, "Capture is not authorized for this website")
    _, _, click, _ = record_click(db, request, link_id, sub_id)
    db.commit()
    return JSONResponse(
        {"click_id": click.id, "campaign_id": offer.id},
        headers={
            "Access-Control-Allow-Origin": origin,
            "Vary": "Origin",
            "Cache-Control": "no-store",
        },
    )


SDK = r"""/* Relay Web Tracking v1. No conversion secret belongs in a browser. */
(function (root) {
  'use strict';
  root.RelayTracking = Object.freeze({
    capture: async function (options) {
      if (!options || options.consent !== true) return {tracked:false,reason:'consent_required'};
      var endpoint = new URL(options.captureUrl);
      if (endpoint.protocol !== 'https:' || !/\/capture\/[a-f0-9]{32}$/.test(endpoint.pathname)) throw new Error('Use your HTTPS capture URL');
      var page = new URL(root.location.href);
      var existing = page.searchParams.get('click_id');
      if (existing && /^[a-f0-9]{32}$/.test(existing)) return {tracked:false,click_id:existing,reason:'existing_click'};
      endpoint.searchParams.set('consent','true');
      endpoint.searchParams.set('page_url',page.origin+page.pathname);
      ['sub_id','source','sub_aff_id','aff_click_id','gclid','gbraid','wbraid','fbclid','utm_source','utm_medium','utm_campaign','utm_content','utm_term'].concat(Array.from({length:10},function(_,i){return 'aff_sub'+(i+1);})).forEach(function(key){
        var value=page.searchParams.get(key); if(value && value.length<=256) endpoint.searchParams.set(key,value);
      });
      var response = await root.fetch(endpoint.toString(), {credentials:'omit',mode:'cors',cache:'no-store'});
      if (!response.ok) throw new Error('Click capture failed: '+response.status);
      var data=await response.json();
      if (!/^[a-f0-9]{32}$/.test(data.click_id)) throw new Error('Invalid click response');
      page.searchParams.set('click_id',data.click_id);
      root.history.replaceState(root.history.state,'',page.toString());
      if(typeof options.onClick==='function') options.onClick(data.click_id);
      return {tracked:true,click_id:data.click_id};
    }
  });
})(window);"""


@router.get("/sdk/relay.js", include_in_schema=False)
def web_sdk():
    return Response(
        SDK,
        media_type="application/javascript",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )
