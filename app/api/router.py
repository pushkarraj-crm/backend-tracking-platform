"""Single registry for public API routers; registration order is intentional."""

from fastapi import APIRouter

from app.features.accounts.router import router as network_account_router
from app.features.auth.router import router as auth_router
from app.features.billing.automation import router as automated_payouts_router
from app.features.billing.invoices import router as invoice_router
from app.features.billing.payouts import router as payouts_router
from app.features.billing.router import router as billing_router
from app.features.callbacks.router import router as callbacks_router
from app.features.campaigns.configuration import router as network_router
from app.features.campaigns.offers import router as offers_router
from app.features.campaigns.router import router as campaign_router
from app.features.conversions.router import router as conversions_router
from app.features.coupons.router import router as coupons_router
from app.features.domains.router import router as domains_router
from app.features.influencer.router import router as influencer_router
from app.features.partners.router import router as core_router
from app.features.platform.router import router as platform_access_router
from app.features.portals.router import router as portals_router
from app.features.reporting.router import router as reporting_router
from app.features.reporting.summary import router as summary_router
from app.features.sources.router import router as sources_router
from app.features.stores.router import router as stores_router
from app.features.tools.automation import router as automation_router
from app.features.tools.router import router as network_tools_router
from app.features.tools.smart_links import router as smart_links_router
from app.features.alerts.router import router as alerts_router
from app.features.teams.router import router as teams_router
from app.features.tracking.landing import router as landing_router
from app.features.tracking.links import router as links_router
from app.features.tracking.rules import router as tracking_router
from app.features.tracking.web import router as web_tracking_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(core_router)
router.include_router(offers_router)
router.include_router(links_router)
router.include_router(conversions_router)
router.include_router(summary_router)
router.include_router(portals_router)
router.include_router(billing_router)
router.include_router(payouts_router)
router.include_router(tracking_router)
router.include_router(domains_router)
router.include_router(reporting_router)
router.include_router(automated_payouts_router)
router.include_router(campaign_router)
router.include_router(invoice_router)
router.include_router(landing_router)
router.include_router(network_router)
router.include_router(network_account_router)
router.include_router(web_tracking_router)
router.include_router(callbacks_router)
router.include_router(platform_access_router)
router.include_router(network_tools_router)
router.include_router(smart_links_router)
router.include_router(automation_router)
router.include_router(influencer_router)
router.include_router(sources_router)
router.include_router(coupons_router)
router.include_router(stores_router)
router.include_router(alerts_router)
router.include_router(teams_router)
