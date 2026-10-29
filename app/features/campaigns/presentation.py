from app.core.config import get_settings

settings = get_settings()


def output_offer(offer):
    from app.tenancy.service import company_context

    company = company_context.get()
    result = {
        key: getattr(offer, key)
        for key in (
            "id",
            "name",
            "destination_url",
            "revenue",
            "payout",
            "currency",
            "active",
            "advertiser_id",
            "attribution_hours",
        )
    }
    result["revenue"] = str(offer.revenue)
    result["payout"] = str(offer.payout)
    prefix = "/c/" + company.slug if company else ""
    result["postback_url"] = (
        settings.public_base_url.rstrip("/") + prefix + "/postbacks/" + offer.id
    )
    return result
