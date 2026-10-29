# PRD implementation baseline

Source: user-supplied PRD version 1.0, dated 2026-10-21 (23 pages).

## Accepted scope

The PRD supersedes earlier conflicting decisions: subscribing brands are tenants;
wallets, funding and provider payouts are required; influencers and commerce
attribution are in scope. This does not connect the unrelated ecommerce project.
User override on 2026-10-21: defer ADM-4 two-factor authentication for now. Keep
email/password authentication. Do not silently re-enable MFA during this work.

Existing network/advertiser accounts and financial history must be migrated
deliberately, not deleted or automatically reclassified. Existing provider jobs
remain disabled until a new financial workflow is implemented and tested. Never
reactivate retired jobs against historical balances.

## Initial code review and outstanding work

This is an implementation backlog, not a statement of production readiness.
Existing functionality must pass acceptance tests before a requirement is marked
complete. Provider setup without credentials is configuration-only.

| PRD requirements | Existing foundation | Required work / verification |
| --- | --- | --- |
| TRK-1–5,10,13 | Links/sub-IDs, SDK, clicks, postbacks, deduplication, diagnostics | Verify first-party storage/consent and pixel fallback end to end; signed ingestion coverage |
| TRK-6–7 | Click-based attribution and lookback | Coupon-only attribution, explicit coupon/first/last-click policy and conflict tests |
| CMP-1–4 | CPL/CPS, goals, conditional rates, publisher overrides | Hybrid model, vertical templates, SKU/category/customer/volume pricing and quality tiers |
| CMP-5–7 | Period caps, campaign access/applications | Budget accounting, lock windows and automatic validation rules |
| CMP-11 | Cloning added in current work | Templates and frontend acceptance coverage remain |
| CNV-1–3,6–7 | Conversion log, review/status APIs and named events | Bulk CSV, explicit lead quality/reasons, full PRD state machine |
| CNV-4–5 | Rejection/fraud checks | Full/partial return/cancel/RTO reversals, hashed lead deduplication, validation flags |
| FRD-1–4,7 | Baseline rule/fraud evaluation | Verify scoring coverage; provider signals need credentials and measured efficacy |
| AFF-1–3,5 | Admin-created partners and account roles | Tenant signup applications, invitations/approval, encrypted KYC profile, provider verification and influencer profile |
| PAY-1–7 | Legacy financial records and external-payment invoices | New append-only ledger, prepaid wallet, signed top-ups, reservations, reconciliation, maker-checker, payout thresholds/cycles, provider jobs, tax documents |
| RPT-1–3,6–7 | Scoped reports and CSV/reporting API | Product/funnel dimensions, XLSX, scheduled delivery and metric reconciliation |
| INT-1,2,4,6 | API, durable outbound callbacks, provider setup drafts | Store plugins, offline CSV ingestion, bank/payout integration and verified setup guides |
| ADM-1,3,5–6 | Roles/staff permissions, audit, separate platform console | Finance/analyst/read-only mappings, KYC/fraud queues, retention/deletion tooling |
| ADM-4 | Email/password | 2FA explicitly deferred by user; SSO P1 |
| TEN-1–10,12,15–16,18–20 | Company directory, optional isolated databases, domain verification and subscription checks | Brand onboarding/migration, editable full-company slug, exact-host credentials, portal custom domains/TLS, plan entitlements, lapse/read-only/retention policy, provisioning recovery |
| Non-functional requirements | Test suite and load-test script | Measure 5,000 clicks/s, 500 conversions/s, latency, recovery and availability on production architecture; free preview hosting is not evidence |

## Current delivery: campaign clone

`POST /campaigns/{id}/clone` accepts `{"name":"New campaign"}`. It requires
campaign management permission, subscription entitlement, quota and source
ownership. It retains the same advertiser and copies configuration: profile,
targeting, events, commission rules, creatives, destinations, traffic rules and
period-cap definitions. The copy starts paused with a new one-time postback key.
It does not copy external IDs, publisher approvals, links, domain assignments,
conversion/payment history, callback credentials or used cap counters. Review
copied settings before activation. A separate audit entry references the source.

## Current delivery: MIS conversion upload (CNV bulk CSV)

`POST /tools/conversions/import` applies an advertiser status report (CSV) to one
campaign. The body names the reference, status and optional click-ID and amount
columns, and maps each status value to `{event_key, status?}`; unmapped values are
ignored. A row updates the conversion with that reference and event, or creates it
from the row's click ID or the click of an earlier conversion with the same reference.
Rows without either are reported as unmatched. Approving a later stage (such as
disbursal) also approves that reference's pending lead; a rejected lead stays rejected. Offline rows skip the attribution
window and minimum-conversion-time checks. `dry_run` (default) previews exact
results; an import with any row error changes nothing. Re-uploading a cumulative
report is idempotent. Mappings are not saved yet, and uploads are bounded by the
64 KB request limit (about 600 rows). The Frontend exposes it under Network tools →
MIS upload.

## Current delivery: global postback (project 1 of the account-types design)

`GET|POST /pb/{account_token}` accepts `click_id` (required), `event`, `txn_id`,
`amount`, `currency`, `status`, `coupon` and `adv_sub1–5`; the click identifies the
campaign. It shares ingestion, receipts and IP allowlists with the per-campaign
endpoint, which keeps working. `status` updates an existing conversion; approving a
later stage approves the pending lead. `amount` is ignored for CPL events. The secret
is created on first view of `GET /postback-settings` and rotated by an admin with
`POST /postback-settings/rotate`. Links also accept `deviceid`, `androidid`,
`googleaid` and `iosidfa` (stored) and forward `t_*` values without storing them.
See `docs/superpowers/specs/2026-10-29-account-types-and-global-postback-design.md`.

Platform templates (project 2): the Postbacks page builds the partner-postback URL
for Trackier, Offer18, Affise, Everflow and Impact (macros verified 2026-10-29 against
each help centre, listed in Frontend `src/platform-templates.ts`) or custom macros.
Templates omit `event` and `status` because those platforms' values do not match
Relay's. AppsFlyer requires Relay to be a registered integrated partner and Adjust
needs a partner setup, so neither is offered yet. `currency` is only checked when
`amount` prices the event.

## Delivery order

1. Campaign setup/templates/cloning and conversion ingestion/attribution.
2. Immutable commission ledger, reversals and wallet accounting with concurrency tests.
3. Funding/payout orchestration, maker-checker, reconciliation and provider setup.
4. Brand tenant onboarding/domains and account roles, retaining historical data.
5. Influencer/KYC/store connectors, reporting, retention and operational verification.

No live fund transfers, paid hosting upgrades, new provider accounts or production
credentials are implied by implementing these flows. Use provider sandbox mode
until separately configured and verified.
