# Campaign workspace delivery — 2026-10-21

## Delivered from the Trackier / Offer18 references

- Campaign names open the corresponding workspace directly.
- Overview cards show status, commission, access and categories. The overview
  includes campaign details, an actionable launch checklist and pause/activate.
- Tabs use product labels: General, Targeting and fraud, Events, Payout rules,
  Caps, Landing pages, Creatives, Traffic rules, Publishers, Test destination.
  Manager-only tabs cannot be opened through a publisher URL parameter.
- Conditional payout and traffic rules use labelled field/operator/value rows,
  including lists, empty checks and numeric comparisons. JSON entry is not
  required for these conditions. Pricing inputs follow the selected CPL/CPS model.
- Landing restrictions accept comma-separated country codes and publisher
  selections. Existing selections outside the loaded account page are preserved.
- Payout rules, landing pages, creatives, traffic rules and caps can be edited.
  Updates preserve disabled state. Configuration is scoped to tenant, campaign
  and authorized actor. Cap metric/scope/period/timezone remain immutable to avoid
  reinterpreting accumulated usage; name and maximum can be updated.
- Cap usage displays the current bucket and a progress bar. The existing 80%
  notification threshold is now configurable from General (1–100%). Threshold
  notifications retain their existing crossing-based behavior. Changing settings
  does not retroactively send notifications. Raising a cap preserves usage.
- Reports offer campaign selection by name, collapsible advanced attribution
  filters, selectable display columns and an equal-duration previous-period
  comparison for clicks/conversions. Currency groups stay separate; day/hour
  buckets align by relative period position. Each query retains the existing
  maximum of 1,000 groups. CSV exports retain all server-provided columns.
- Sidebar navigation starts with Overview and Campaigns, followed by the campaign
  workspace and daily operations; account tools and settings follow.
- Mobile campaign forms stack vertically; campaign tabs scroll horizontally;
  tables scroll within their panels instead of widening the page.

## Backend contracts

Added PUT alongside existing POST/PATCH for:

- `/campaigns/{campaign_id}/commission-rules/{rule_id}`
- `/campaigns/{campaign_id}/destinations/{destination_id}`
- `/campaigns/{campaign_id}/creatives/{asset_id}`
- `/campaigns/{campaign_id}/traffic-rules/{rule_id}`
- `/campaigns/{campaign_id}/caps/{cap_id}`

`settings.cap_alert_percent` lives in the existing validated campaign JSON
configuration, so this increment does not require a schema migration. Existing
campaigns default to 80%. Cap listing includes `alert_percent` for display.

## Verification

Final checks: 49 frontend tests and 27 campaign backend integration tests passed.
TypeScript, frontend/backend lint, and the production build passed.

- React/TypeScript regression coverage includes condition serialization, editing
  landing restrictions, preserving selections across paginated account lists,
  pricing-model changes, publisher access restrictions, cap progress, previous
  period dates, currency separation, zero baselines and time-bucket alignment.
- API integration coverage includes all five new editing endpoints, disabled
  state preservation, cross-campaign/tenant isolation, cap scope protection,
  configured threshold crossing and retaining counters when increasing a limit.
- Headless Edge was run against Vite and a real FastAPI server using a temporary
  SQLite database, separate from the working database. It verified campaign
  creation, workspace navigation, condition creation, payout editing, campaign
  activation, targeting save, an attributed click, an approved conversion,
  reporting, comparison and column selection. Desktop and 390px mobile layouts
  were inspected; there was no page-level horizontal overflow or browser runtime
  error in the exercised flows.
- This browser run used local test accounts and no external providers. PostgreSQL
  concurrency/load, provider delivery and production hosting were not exercised.

## Reference work still outstanding

This is a working increment, not full product parity. The larger backlog remains
in `product-demo-references.md` and the accepted PRD. In particular: coupon
attribution and quotas, product feeds, conversion CSV ingestion, full financial
ledger/reversals/wallets, nested landing fallback trees, direct creative uploads,
provider-backed VPN intelligence, mobile SDK/MMP adapters, expanded automation,
partner sync, mailer, portal terminology and configurable signup fields.

No production deployment, live messages, provider activation or fund transfers
were performed.
