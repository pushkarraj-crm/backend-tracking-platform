# Offer18 reference review and implementation scope

Reviewed the public [Offer18 knowledge base](https://knowledgebase.offer18.com/)
and its 162-page English documentation index on 19 October 2026. The source is
a functional reference; this project retains its own code, UI and business model.

## Product decisions that take precedence

Each network owns its advertisers and publishers. Production company mode retains
its own dashboard hostname and PostgreSQL database. Publishers may have an optional
parent affiliate. Advertisers pay publishers externally through invoices; the SaaS
never initiates publisher fund transfers. Subscription billing remains Razorpay with
a 15-day trial. No MFA/OTP is introduced. Ecommerce platform plugins, iGaming-specific
accounting, reviewer cloaking, raw personal-data URL macros and probabilistic device
fingerprinting are outside this agreed product. No external provider credentials
have been supplied: build draft configuration and setup guidance, not fake live
connections or unverified provider deliveries.

## Feature map

| Area | Existing | Required extension |
|---|---|---|
| Companies/accounts | Company provisioning, JWT sessions, roles, publisher hierarchy | Scoped partner API keys; restrictive employee permissions; network branding |
| Campaign catalog | CPL/CPS, applications, pause, terms in description | Open/approval/private visibility; editable profile; categories, preview URL, terms, internal notes |
| Tracking | Durable IDs, redirects, ad URL parameters | Stored source/sub IDs and affiliate click ID; query macros; allowlisted deep links; weighted destinations; impressions; consent-gated redirectless web helper |
| Conversions | One conversion/click, transaction replay protection | Named goals; opt-in repeatable events; event/publisher/traffic-specific commission rules; preserved financial snapshots |
| Targeting/fraud | Country/device/OS/language/schedule, IP velocity, bots, network blocks | Ordered typed conditions; per-event actions; IP restrictions for authenticated advertiser postbacks |
| Caps | Atomic UTC daily/total click/conversion/revenue caps | Publisher/event-scoped period caps and timezone-aware hourly/weekly/monthly buckets; visible usage |
| Destinations/creatives | Hosted escaped landing pages; verified advertiser domains | Approved external landing variants; weighted routing; safe creative URL references and publisher-specific HTML snippets |
| Partner callbacks | Signed global JSON callbacks, durable retries | Campaign overrides, GET/POST templates, test events, partner-scoped delivery logs and retries |
| Diagnostics | Audit, fraud and staff delivery logs | Redacted advertiser postback receipts; destination/link preview without creating financial events |
| Reporting | Filtered detail/grouped reports, bounded safe CSV exports | Named-event/source/sub-ID filters; distinct-click aggregation under multi-event attribution; saved templates; impression KPIs |
| Operations | Billing maintenance and domain revalidation | In-app workflow notifications; bulk campaign import preview/apply; configuration-only external integration catalog and guides |
| Payments | Direct invoices, reports, disputes and receipt confirmation | Preserve this workflow; do not restore wallets or automated payouts |

## Verification contract

Every extension must have workspace/party authorization, bounded inputs, durable
database constraints and relevant PostgreSQL tests. Financial changes must preserve
idempotency, invoice reservations and immutable commission lines. Browser API keys
must not become public conversion credentials. New trackers must not serve different
content to ad reviewers. Frontend code remains in the separate frontend repository.
Deploy backend before frontend through the existing free-tier CI/CD pipelines.

Actual production DNS/TLS, company database provisioner credentials, provider
activation, live billing verification and realistic load tests remain external
configuration/verification prerequisites; documentation review does not establish
those capabilities or advertising-platform approval.

## Reference sections

- [Campaign settings](https://knowledgebase.offer18.com/network/offers/offer-general)
- [Conversion events](https://knowledgebase.offer18.com/network/offers/offer-events)
- [Commission rules](https://knowledgebase.offer18.com/network/offers/offer-payout-rules)
- [Capping rules](https://knowledgebase.offer18.com/network/offers/offer-capping-rules)
- [Tracking parameters](https://knowledgebase.offer18.com/affiliate/affiliate-tracking-url-parameters)
- [Partner postbacks](https://knowledgebase.offer18.com/network/affiliates/affiliate-global-postback)
- [Reporting templates](https://knowledgebase.offer18.com/network/reports/reports-templates)
- [Notifications](https://knowledgebase.offer18.com/network/account-configuration/notifications)

Implementation status and remaining verification are recorded with each release;
unimplemented reference features must not be represented as available.


## Implemented extension

The extension is implemented in the backend and separate React frontend:

- Campaign profile and visibility, direct publisher assignment, named CPL/CPS
  events, repeated goals and optional automatic approval.
- Prioritized commission/traffic rules, timezone and publisher/event caps,
  weighted destinations, deep links, creative snippets and weighted smart links.
- Source/sub-ID metadata, impression records, consent-gated web capture and
  authenticated S2S conversion attribution.
- Campaign/global signed GET/POST callbacks, safe test events, scoped delivery
  retries and redacted advertiser receipt logs.
- Scoped partner API keys, restrictive employee permissions, network branding,
  personal report templates and in-app notifications.
- CSV import preview/atomic apply, invoice-safe bulk review, URL preview,
  public HTTPS health checks and bounded notify/pause automation.
- Owner-scoped encrypted provider drafts and original setup/API guides.

Local verification: existing regression suite plus 14 new PostgreSQL integration
scenarios passed together (69 passed; one optional Redis integration skipped).
Two additional GET-signature/TLS-pinning and daylight-saving bucket tests passed
separately. The frontend has 32 passing tests; lint, TypeScript/build and dependency
audits pass. Browser QA saved, reloaded and disabled a named event in an isolated
company database. CI additionally runs Linux PostgreSQL/Redis and a Docker build.
Deployment success is recorded by the hosting pipelines, not inferred from local
checks. Representative production load and real external-provider delivery have
not been verified.

External adapters remain disabled by design. No ecommerce plugin, SDK fingerprint
matching, arbitrary HTML/page builder, automated publisher transfer, SMS/OTP or
reviewer cloaking was added. Creative uploads and external notification sends
require a separately implemented provider adapter; they are not represented as
working merely because their configuration can be saved.
