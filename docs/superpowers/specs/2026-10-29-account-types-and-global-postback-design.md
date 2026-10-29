# Account types, global postback and platform connections — design

Status: draft for owner approval · 2026-10-29
Repos touched: Backend, Frontend (Super Admin only for account-type visibility).

## 1. Intent

Relay is an affiliate tracking platform (an Offer18 / Trackier competitor) for CPS and
CPL in three verticals: **fintech** (loans, cards, accounts — multi-stage CPL funnels),
**D2C / ecommerce** (CPS) and **influencer marketing**. The product is in development,
with no production customers yet.

Today every workspace gets the same network-shaped app, each campaign needs its own
postback URL and secret, and nothing serves brands that manage agencies or creators
that track their own brand deals. This design turns Relay into one product with three
account types, one postback per account, and ready-made connections to the tracking
platforms advertisers already use.

### Who it serves

| Account type | Who signs up | Their problem today |
|---|---|---|
| **Brand** (direct advertiser) | A D2C or fintech brand | Splits a sales goal across several marketing agencies (e.g. 8) and cannot see in one place how each agency performs |
| **Network** | An affiliate network | Today's product; postback setup per campaign is tedious |
| **Influencer** | A creator with existing brand tie-ups | Posts Instagram stories and YouTube videos with links but gets no insight into clicks or sales per brand or per post |

Influencers do **not** need Relay to find brands. They already have deals; Relay is
their tracking tool.

## 2. Goals, non-goals, anti-goals

Goals
1. One global postback URL per account, created automatically at signup.
2. Connection templates so conversions flow in from Trackier, Offer18, Affise, Everflow,
   Impact, AppsFlyer, Adjust, or any platform via a Custom template.
3. Three account types chosen at signup, each with its own navigation, wording, home
   dashboard and feature set, on one codebase.
4. Brand accounts: per-agency links for the same campaign, an overall sales goal split
   into per-agency targets, fixed commission per sale, progress on the dashboard.
5. Influencer accounts: brand deals, one tracked link per post/placement, clicks and
   sales from day one.
6. Offer18-style guided campaign creation with fintech, D2C and influencer templates.
7. Blocked traffic sources: traffic from a blocked source is flagged as fraud and
   earns no commission.
8. Accept Offer18's tracking-link parameter set.
9. Visual redesign (MoneyFlow look, Offer18 content) across all three dashboards.

Non-goals
- No Instagram / YouTube / any social-platform API, views or story data. Ever. Only
  data from Relay's own links and from advertisers' reports/postbacks.
- No influencer marketplace or brand discovery.
- No pulling data from other platforms' APIs; connections are S2S postbacks only.
- No light/dark theme switch in this round.
- No change to the existing payout/ledger work (PRD steps 2–3).

Anti-goals (would be failure even if it works)
- Mixing one account's data into another's. Account isolation, including the optional
  per-company database mode, must hold for every new feature.
- Breaking existing per-campaign postback URLs that advertisers already fire.
- Storing personal data passed through links (`t_email`, `t_phone`, …).
- Three diverging copies of shared screens (reports, campaigns, conversions).

## 3. Constraints and existing system

- FastAPI + PostgreSQL backend; React/Vite frontend; separate Super Admin app.
- Tenancy: rows carry `workspace_id`; optional "company databases" mode gives each
  company its own database and hostname, resolved through a control-plane directory
  (`app/tenancy.py`). New lookups by public token must work in both modes.
- Roles: `admin`, `staff`, `affiliate` (can create sub-publishers), `publisher`,
  `advertiser`, plus `platform_admin`.
- `Workspace.business_type` already records the signup choice
  (`brand_direct_advertiser`, `affiliate_ad_network`, `agency`, …).
- `TrackingLink` is unique per (campaign, partner). Clicks already store `sub_id` and
  tracked parameters (`aff_click_id`, `sub_aff_id`, `aff_sub1–10`, `source`, `utm_*`,
  `gclid`, `fbclid`, …). Deep links (`url`) work when enabled, restricted to approved hosts.
- Conversions are ingested by `conversion_engine.ingest` (dedupe, attribution window,
  caps, pricing rules, traffic rules, status transitions, commission entries, partner
  webhooks). Every new conversion source must go through it.
- Existing postback: `POST /postbacks/{campaign_id}` with JSON body and
  `X-Postback-Key` header; receipts logged for diagnostics; optional IP allowlist.
- MIS/CSV conversion upload exists (`POST /tools/conversions/import`, built 2026-10-23).
- Traffic rules (`TrafficRule`, phase click/conversion, action approve/reject/pending)
  and fraud events exist. Referrer is **not** captured today.
- Period caps exist (per campaign, optionally per partner); they are hard limits.

## 4. Approaches considered

1. **One app, account type on each workspace** — chosen. One backend, one frontend;
   the account type selects navigation, wording, dashboard and enabled features.
   Tracking, postbacks, uploads, fraud and reports are written once.
   Trade-off: every screen must respect the account type.
2. Separate frontend apps per account type on one backend. Fully tailored UIs, but
   shared screens built and maintained three times.
3. Separate products with separate backends. Duplicates the tracking engine; rejected.

Chosen by the owner on 2026-10-29: the three types differ mostly in what users see and
what things are called, not in how tracking works.

## 5. Delivery order

Each project is planned and shipped separately, in this order (each builds on the last):

1. Global postback + tracking-link parameters — done 2026-10-29
2. Platform connection templates — done 2026-10-29
3. Account types and dashboards (brand targets, influencer workspace) — done 2026-10-29
4. Offer18-style campaign creation with vertical templates — done 2026-10-29
5. Blocked traffic sources (plus Offer18 anti-fraud gaps: unique-click limit, proxy detection) — built 2026-10-29; proxy detection not built (needs an IP-reputation data source)
6. Redesign

Added 2026-10-29 from the [Offer18 function review](../../offer18-function-review.md) (P1 gaps),
each a separate project to schedule after the list above unless the owner reorders:

7. Coupons: codes per campaign and partner, validity and quota, coupon-only
   attribution, coupon reports and CSV import.
8. Shopify and Indian checkouts (Shiprocket, GoKwik, Razorpay Magic Checkout, Flexype)
   and WooCommerce, via a conversion pixel / Web SDK.
9. Capping over-actions (pending, accept, reject, stop campaign, fallback) and fallback
   to another campaign.
10. Revenue-share model: commission derived from the advertiser's reported payout.
11. Telegram and Slack alerts for caps, postback errors and fraud.
12. Login as partner, agency team logins and account managers.
13. AppsFlyer integrated-partner registration (business step) before building
    fintech-app attribution.

## 6. Project 1 — Global postback and link parameters

### 6.1 Endpoint

```
GET|POST https://<tracking-domain>/pb/{account_token}
  ?click_id=…&event=…&txn_id=…&amount=…&currency=…&status=…&coupon=…&adv_sub1…adv_sub5=…
```

| Parameter | Required | Meaning |
|---|---|---|
| `account_token` (path) | yes | Secret per account, generated at signup, rotatable, shown only to admins |
| `click_id` | yes | Relay click ID; identifies campaign, partner and link |
| `event` | no | Campaign event code (`lead`, `approved`, `disbursed`, `sale`, `install`…). Default: the campaign's initial event |
| `txn_id` | recommended | Advertiser's order/application ID. Dedupe key; also the MIS upload reference |
| `amount` | CPS | Sale amount for percentage commission |
| `currency` | no | ISO code; must match the campaign currency (existing rule) |
| `status` | no | `approved` / `rejected` / `pending`; lets the advertiser update a conversion later |
| `coupon` | no | Coupon code used; stored on the conversion for influencer reporting |
| `adv_sub1–5` | no | Stored as today |

POST accepts the same fields as form or JSON. Parameter names are accepted
case-insensitively.

Behaviour
- Resolve the account from the token (constant-time hash comparison, as today). In
  company-database mode the control-plane directory maps the token to the company.
- Find the click inside that account only; a click from another account is "not found".
- Route through `conversion_engine.ingest` unchanged: dedupe, attribution window,
  caps, pricing, traffic rules, auto-approve, commission, partner webhooks.
- `status` on a repeat of an existing (`txn_id`, `event`) applies a status transition
  (same rules as the MIS upload, including "approving a later stage approves the pending lead").
- Responses: `201` created, `200` duplicate, `4xx` with a short machine-readable reason.
  Every request writes a postback receipt (existing diagnostics), with the token never logged.
- Campaign-level protections still apply: per-campaign IP allowlist, attribution window.
- The token must be redacted from access logs and error reports (it sits in the URL).

Migration
- Existing workspaces get a token on deploy. Signup creates one.
- `POST /postbacks/{campaign_id}` with `X-Postback-Key` keeps working unchanged.
  Campaign screens show the global URL first and the legacy URL under "Advanced".

### 6.2 Passing the click ID to the advertiser

Each campaign chooses the destination parameter that carries Relay's click ID
(default `click_id`; e.g. `aff_sub`, `subid`, `p1`). The redirect appends it using the
existing destination macro mechanism.

### 6.3 Tracking-link parameters (Offer18 set)

Already accepted: `aff_click_id`, `sub_aff_id`, `aff_sub1–10`, `source`, `url`.
Add:
- `deviceid`, `androidid`, `googleaid`, `iosidfa` — stored on the click, forwarded to
  the destination and to app-tracker templates.
- `t_first_name`, `t_last_name`, `t_email`, `t_phone`, `t_address`, `t_sub1–10` —
  **forwarded to the destination only; never stored, logged or shown in reports.**
  Values travel only in the redirect URL. Rationale: personal data under DPDP with no
  reporting value; Offer18 also does not store them.

## 7. Project 2 — Platform connection templates

A campaign (or advertiser) can say "conversions come from: Trackier | Offer18 | Affise |
Everflow | Impact | AppsFlyer | Adjust | Custom".

A template defines:
1. which outgoing parameter on the advertiser link carries Relay's click ID on that
   platform (its partner sub-ID), and
2. the partner-postback URL to paste into that platform: the account's global
   postback with that platform's macro names in place of Relay's values.

The screen shows the ready-to-paste URL with a copy button and short setup steps.
Custom lets the user type the macros for each Relay parameter.

Rules
- Templates are data (a versioned catalog), not code per platform.
- **Macros must be verified against each platform's current documentation at build
  time, not written from memory.** A platform that cannot send a usable partner
  postback is reported to the owner instead of shipped. Impact (network-like) and
  AppsFlyer/Adjust (app install/in-app events) are the likeliest exceptions.
- No API credentials are stored for these templates.
- The existing "Integrations" drafts (Meta, Google, …) are a different, outbound
  feature and stay as they are.

## 8. Project 3 — Account types and dashboards

### 8.1 Account type

New `Workspace.account_type`: `brand` | `network` | `influencer`, required at signup.
Existing workspaces: `brand_direct_advertiser` → `brand`, everything else → `network`.
Signup gets an "Influencer / creator" choice. The type is fixed after signup except by
the platform owner (Super Admin), who can change it.

The frontend reads the type from the session and selects navigation, labels and the
home dashboard. The backend enforces feature access by type (UI hiding is not security).

### 8.2 Brand

- The brand is its own and only advertiser: Advertisers screens are hidden; campaigns
  are created without choosing an advertiser.
- Partners are labelled **Agencies** (existing `affiliate` role; they keep the ability
  to create sub-publishers and see only their own data) plus affiliates/influencers.
- Each agency gets its own link for the same campaign (existing behaviour).
- **Sales targets** (new): a campaign has an optional overall goal (e.g. 5,000 sales of
  event `sale`) split into per-partner targets (Agency A: 800). Commission per sale is
  the campaign's existing pricing (e.g. ₹80).
  - Targets are goals for tracking, **not limits**: sales beyond a target still count
    and earn commission. Brands that want a hard stop use the existing caps.
  - Targets have a date range. Sum of partner targets may differ from the overall goal;
    the UI shows the difference.
- Brand dashboard: overall progress (3,140 / 5,000), cost so far (sales × commission),
  per-agency progress, clicks, conversion rate, trend, and who is ahead/behind the
  straight-line pace for the date range.

### 8.3 Network

Today's product, plus the global postback, templates and redesign. No functional change.

### 8.4 Influencer

- The influencer is the workspace admin and also the partner on their own links.
- **Brand deals** = campaigns whose advertiser is a simple brand record (name, logo,
  website) without a login. Deal terms use existing pricing: ₹ per lead, % per sale,
  or a fixed fee recorded for reporting.
- **Placement links**: many named links per deal ("Insta story 28 Oct", "YouTube
  review", "Link in bio"), each a short URL. Because `TrackingLink` is unique per
  (campaign, partner), a placement is a named sub-ID on the influencer's link; the
  short URL carries it. Reports group by placement and by platform (a placement field
  chosen from Instagram, YouTube, Other).
- **Coupon code** per deal, stored and matched on postback/upload `coupon`.
- Sales sources: global postback (6.1), report upload (existing MIS upload, matching
  click ID, txn_id or coupon), and **manual entry** of period totals per deal
  ("October: 42 sales, ₹18,400"). Manual totals have no click and are stored separately
  from conversions; dashboards add them to totals and label them "reported by brand".
- Influencer dashboard: clicks, sales, earnings, conversion rate by brand, placement,
  platform and day.

### 8.5 Access summary

| Feature | Brand | Network | Influencer |
|---|---|---|---|
| Advertisers screens | hidden | yes | hidden (brands are simple records) |
| Partners / sub-publishers | agencies, affiliates, influencers | publishers, affiliates | none |
| Sales targets | yes | optional later | no |
| Placement links | no | no | yes |
| Manual sales entry | no | no | yes |
| Global postback, templates, MIS upload, fraud rules | yes | yes | yes |

## 9. Project 4 — Campaign creation (Offer18-style)

Replace the create-campaign pop-up with a full guided page modelled on Offer18's
campaign setup: sections for basics, destination & click-ID parameter, conversion
source (global postback + platform template), events & payouts, targeting, caps,
targets (brand), creatives, and review.

Templates pre-fill events and pricing:
- **Fintech CPL funnel**: `lead` → `approved` → `disbursed` (or `card_activated`),
  payout per stage, later-stage approval approves the lead.
- **D2C CPS**: `sale` with % commission, returns/RTO lock window placeholder (until the
  ledger work lands).
- **Influencer deal**: brand record, coupon code, placements.

The exact Offer18 field list is gathered from its documentation/demo when this project
is planned.

## 10. Project 5 — Blocked traffic sources

- Capture the click's referring domain (hostname only; no full URL, no query) on the
  click record.
- A "Blocked sources" screen per account and per campaign with presets: Google search
  ads (gclid present), Meta ads (fbclid), cashback/coupon sites, Telegram/WhatsApp,
  pop/redirect traffic, plus custom domains and `source`/`utm_source` values.
- Presets compile to existing traffic rules (reject action), so enforcement uses the
  existing engine.
- A rejected click/conversion creates a fraud event with the reason; it earns no
  commission; the partner sees the reason.
- Documented limit: detection relies on referrer and parameters that browsers or
  dishonest partners can hide; it catches common cases, not determined fraud.

## 11. Project 6 — Redesign

- Look: MoneyFlow reference — dark navy sidebar with violet active item, light page,
  pastel-tinted KPI cards with round icons and sparklines, donut + legend, area trend
  chart with tooltip, progress cards, coloured status pills, gradient banner.
- Content: Offer18 dashboard content adapted per account type (mockup:
  `.superpowers/brainstorm/978-1793269398/content/home-dashboard.html` in the parent folder).
- Delivered as shared design tokens/components first (every screen restyles), then the
  three home dashboards, then other screens one by one.
- Relay keeps its own name and logo; no copying of Offer18/MoneyFlow branding.

## 12. Decisions made by Claude (owner to check)

- Postback URL shape and parameter names (6.1); token in the path.
- `t_*` parameters are forwarded but never stored.
- Targets are goals, not limits; hard stops use existing caps.
- Existing workspaces map to `brand` only from `brand_direct_advertiser`.
- Influencer is the partner on their own links; placements are named sub-IDs.
- Manual sales totals are stored apart from conversions and labelled.
- Platform templates are verified against live docs, and unworkable ones are reported, not shipped.

## 13. Left to the builder

- Table and column names, migrations, API route names beyond `/pb/{token}`.
- Token format/length and rotation UX.
- Exact fraud preset lists (e.g. which cashback domains).
- Component structure for the redesign.
- Test strategy per project (each project ships with backend tests on PostgreSQL and
  frontend tests, following the repos' existing checks).
