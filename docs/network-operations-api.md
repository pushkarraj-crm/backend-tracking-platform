# Network operations API

Use authenticated JSON requests through the existing company-aware API proxy.
Authorization is always the intersection of network membership, account role,
resource ownership and optional employee/API-key restrictions. A URL or ID never
grants access by itself. Decimal money values are represented as strings.

## Campaign configuration

The frontend's **Campaign operations** page selects a campaign from the scoped
catalog and exposes these routes. Existing campaign creation, targeting rules,
applications, domain configuration and invoice workflows remain available.

| Routes | Purpose and access |
|---|---|
| `GET/PUT /campaigns/{id}/profile` | Visibility, categories, terms, preview URL, internal notes, safe deep links, initial-event policy and optional postback IP ranges. Only owners/network management write; publishers receive redacted settings. |
| `POST /campaigns/{id}/assign` | Assign/revoke a publisher's access; permits private-campaign assignment. |
| `GET/POST /campaigns/{id}/events`, `PUT .../events/{event_id}` | Named CPL/CPS goals, independent payout, opt-in repeated transactions and auto approval. Event code is immutable. |
| `GET/POST /campaigns/{id}/commission-rules`, `PATCH .../{rule_id}` | First matching active rule ordered by numeric priority then ID. Optional publisher/event scope and typed AND conditions. Deactivate and replace a pricing rule to preserve its history. |
| `GET/POST /campaigns/{id}/caps`, `PATCH .../{cap_id}` | Gross click/conversion/revenue/payout caps, optional publisher/event, hour/day/week/month/total, IANA timezone, historical usage and atomic counters. Rejected conversions still count as gross traffic; these are not approved-only caps. |
| `GET/POST /campaigns/{id}/destinations`, `PATCH .../{destination_id}` | Weighted landing variants with optional country and approved-publisher scope. |
| `GET/POST /campaigns/{id}/creatives`, `PATCH .../{asset_id}` | Bounded HTTPS asset references; no arbitrary HTML upload or file-storage adapter. |
| `GET .../creatives/{asset_id}/snippet` | Publisher's own escaped sponsored-link HTML and impression URL; requires active approved tracking access. |
| `GET/POST /campaigns/{id}/traffic-rules`, `PATCH .../{rule_id}` | Ordered conditions: click allow/reject or conversion pending/approve/reject. Cannot bypass base fraud checks, tenant isolation or hard caps. |
| `POST /campaigns/{id}/url-preview` | Macro preview for an existing publisher link, without recording a click or conversion. Simulated country/device values are preview-only. |

Configuration collections are bounded to 100 records per campaign. Conditions use
`{"all":[{"field":"source","operator":"eq","value":"search"}]}`. Supported
fields cover country/device/OS/language, source, placement and affiliate sub IDs,
publisher, event, sale amount, click-to-conversion seconds, weekday and advertiser
sub parameters. `in/not_in/between` require arrays; other operators require scalars.
No arbitrary SQL, executable expressions or regular expressions are accepted.

Financial attribution is immutable at ingestion: the conversion stores its event,
rate source, values and pricing snapshot. Retrying an identical event/transaction
returns its original conversion; conflicting payloads return 409. Enabling repeated
events does not disable transaction deduplication. New earnings remain protected
by invoice reservations and versioned commission entries.

## Reporting and diagnostics

`GET /reports/{clicks|conversions|impressions}` provides scoped detail pages;
`GET /reports/grouped` adds event/source grouping and distinct-click counts.
Multi-goal conversion counts can exceed click counts, so conversion rate uses
`approved_unique_clicks / clicks`, not the number of approved goals. Currency
totals remain separate. Event groups share their attributed click populations;
do not sum their click counts across events. Impressions are separate records,
not inferred from clicks or treated as financial events.

Filters include the existing date/campaign/publisher/country/placement/status
fields plus `event_key`, `source`, `aff_click_id`, `sub_aff_id` and `aff_sub1`–10.
Event/status filters do not apply to impressions. CSV endpoints
`/reports/export/{clicks|conversions|impressions}` retain bounded streaming,
authorization and spreadsheet-formula escaping. Limit the time range when an
export is truncated. Detail report windows use UTC.

`POST/GET /report-templates`, `GET /report-templates/{id}/run` and
`DELETE /report-templates/{id}` store personal relative-window report preferences.
Templates do not grant access to another account's report data.

`GET /postback-receipts` shows authenticated advertiser callback outcomes without
postback keys, full bodies or raw client IPs. Publisher callbacks and delivery logs
are described in [the integration guide](network-integrations.md).

## Accounts and operations

- `PUT /users/{id}/api-access`: network admin enables/disables an account's API
  access. Disabling revokes its keys. `POST/GET /api-keys` and
  `DELETE /api-keys/{id}` create/list/revoke keys through an interactive session.
  Keys expire in at most 365 days, are stored as hashes and are revealed once.
  Scopes: campaigns:read/apply, reports:read, conversions:read, links:write,
  invoices:read. No API-key permission grants billing, account administration or
  arbitrary financial writes. Original ownership and parent-account checks apply.
- `GET /employee-permissions`, `PUT /users/{id}/permissions`: admin restricts staff
  modules; `null` preserves original staff-role permissions, `[]` denies management
  modules. These restrictions never upgrade a staff role to administrator.
- `GET/PUT /branding`: network name, accent, HTTPS logo, support email, timezone
  and optional policy links. Only the network admin writes. Naming does not rename
  an existing company hostname. Policy links do not supply missing legal text or
  satisfy public-registration deployment requirements automatically.
- `GET /notifications`, `POST /notifications/{id}/read`,
  `POST /notifications/read-all`: account-owned in-app workflow notices. No external
  email, Slack or Telegram messages are sent by these routes.
- `POST /tools/import-campaigns`: up to 100 CSV rows / 60 KB; defaults to dry-run.
  Strict headings match campaign creation fields. Invalid rows/quota errors stop
  the entire import. Applying returns postback keys once: store them securely.
- `POST /tools/conversions/status`: up to 100 distinct scoped conversion IDs;
  approve/reject atomically, preserving every invoice reservation.
- `POST /tools/campaigns/{id}/link-health`: owner/network management checks public
  HTTPS with bounded redirects and DNS/TLS address pinning. Records no traffic and
  never claims Meta/Google approval. It may contact the advertiser's destination.
- `GET/POST /smart-links`, `PATCH /smart-links/{id}`, public `GET /s/{id}`:
  weighted approved publisher links for one advertiser. Selection filters basic
  availability and redirects to the normal tracking handler for final fraud and
  cap enforcement. No eligible child returns 410. A selected child reaching a hard
  cap can return its cap response; selection does not promise automatic failover
  across every advanced rule. Custom advertiser tracking hosts are ownership-checked.
- `GET/POST /tools/automation`, `PATCH /tools/automation/{id}`,
  `GET /tools/automation-logs`: hourly bounded checks of clicks, approved goals or
  unique-click conversion rate. Minimum sample, lookback and cooldown are required.
  Actions notify in-app or pause a campaign, never transfer funds or auto-resume.

See [provider configuration and S2S setup](network-integrations.md) and
[reference review](offer18-feature-review.md) for exclusions and external setup.
These are implemented core workflows, not a claim of complete Offer18 feature
parity or verified production capacity.
