# Advanced backend features

All authenticated endpoints use the existing bearer token, tenant isolation and role checks. Login remains email/password only. The frontend is separate; consult `/openapi.json` in development for exact request/response schemas.

## Targeting, caps and fraud

Staff can `PUT /offers/{offer_id}/rules` and read the same path with GET. For example:

```json
{
  "countries": ["IN"],
  "devices": ["mobile"],
  "operating_systems": ["android", "ios"],
  "languages": ["en"],
  "utc_hours": [8, 9, 10, 11, 12],
  "daily_clicks": 10000,
  "total_conversions": 5000,
  "daily_revenue": "25000.0000",
  "block_bots": true,
  "clicks_per_ip_minute": 120,
  "minimum_conversion_seconds": 5
}
```

Optional start/end times must include a timezone. Empty targeting lists accept any value. Daily counters reset by UTC date; total counters persist. Conditional database updates enforce caps under concurrency. Conversion caps count every accepted unique postback, including pending or rejected conversions, and are not released by later rejection. Revenue caps use the offer currency. Migration `0007` backfills historical counters while locking tracking tables: schedule this migration during maintenance on a populated deployment.

Country comes from the server-observed IP using a configured GeoIP Country MMDB, never a client country header. Configure `GEOIP_DATABASE_PATH` and mount the licensed database at that path. Unknown countries fail country-targeted offers. Configure the gateway and Uvicorn trusted proxies correctly. Device/OS/language classification uses request metadata and is heuristic.

Fraud rules detect common bot agents, blocked IP networks, excessive IP velocity, and conversions arriving too quickly. Stored IP identifiers are keyed hashes. `GET /fraud/events` and `POST /fraud/events/{event_id}/review` support staff review. These rules do not guarantee detection of all fraudulent traffic.

## Custom tracking domains

Set `TRACKING_CNAME` to your tracking ingress hostname. Admins create domains with `POST /tracking-domains`; its response supplies a random TXT verification value and the required CNAME. Keep both DNS records installed. `POST /tracking-domains/{id}/verify` checks ownership and routing; list/delete use the same collection. Verification expires after seven days and maintenance renews it before expiry. Failed or expired verification blocks traffic and certificate approval.

Pass `tracking_domain_id` when creating/listing links to produce HTTPS URLs on that domain. Custom hosts serve tracking paths only and must belong to the link's tenant. Caddy uses the protected certificate authorization endpoint and issues certificates only for verified domains. Set a strong `DOMAIN_CERTIFICATE_TOKEN`; do not expose the internal certificate hostname. Actual DNS propagation and certificate issuance require deployment verification.

## Each network's own RazorpayX account

Admin configuration is under `/automated-payouts`: `PUT /provider` accepts the network's RazorpayX key ID, secret, funding account number, webhook secret, minimum amount, schedule interval and `automatic` flag. Credentials are encrypted and omitted from responses. Automatic scheduling defaults to disabled. Each network must complete provider onboarding and fund its own account; your platform does not pool payout funds.

Create beneficiaries in that network's RazorpayX account, then `PUT /beneficiaries` with the affiliate/publisher ID and existing `fa_...` fund account ID. The backend fetches the fund account using that network's credentials before accepting it. The current adapter supports INR via IMPS. Credentials, API access, source IP allowlisting and account capabilities must be verified with RazorpayX.

`POST /requests` accepts a recipient, decimal amount and stable client `request_key`; list requests with GET. Available approved commissions are reserved transactionally before queueing. The worker retries with the same provider idempotency key. Processing transfers retain reservations; processed transfers create one payout ledger entry; reversals create a compensating credit. Signed, deduplicated `/webhooks/{provider_id}` events trigger an authoritative provider fetch, and maintenance reconciles outstanding transfers.

Only a queued request with no submission attempt can be cancelled via `POST /requests/{id}/cancel`. A timeout may have occurred after provider acceptance, so exhausted retries retain the reservation for reconciliation instead of making the money spendable again. Investigate failed delivery records and provider status before recovery. Funding account changes are blocked while transfers are outstanding. Key changes must retain access to existing beneficiaries. Coordinate webhook secret rotation with queued provider deliveries.

The implementation uses Razorpay's [payout API and idempotency header](https://razorpay.com/docs/api/x/payout-wallet/create/payout-composite/). Local tests mock the provider; no bank transfers have been executed during development.

## Reports and exports

`GET /reports/clicks`, `/reports/conversions`, and `/reports/grouped` accept timezone-aware `start`/`end`, offer, affiliate, country, placement `sub_id`, and conversion status filters. The default is seven days and the maximum window is 366 days. Grouping supports offer, affiliate, placement, country, device, OS, UTC day and hour. Role visibility applies to every query; advertiser reports hide payout values, affiliate/publisher reports hide revenue. Money stays separated by currency.

`GET /reports/export/clicks` and `/reports/export/conversions` stream CSV in bounded database batches. `max_rows` defaults to 10,000 and cannot exceed 100,000. Check `X-Export-Rows`, `X-Export-Max-Rows` and `X-Export-Truncated`; split large exports into smaller time ranges. CSV values are protected against spreadsheet formula injection.

## Billing lifecycle and operations

Razorpay subscription checkout schedules the first charge after the remaining 15-day trial. Signed/deduplicated webhooks reconcile current provider state, preventing delayed events from overriding newer state. Billing APIs also support pause, resume, cancellation, reconciliation and invoice listing. Pending renewal receives a configurable grace period (`BILLING_GRACE_HOURS`, default 72) anchored to the previous paid period; repeated events cannot indefinitely extend it. Paused, halted, expired, completed and cancelled subscriptions stop entitlement. Trial expiry and suspended tenants are enforced separately.

Run both `python -m app.worker` and `python -m app.maintenance` alongside the API. Maintenance schedules opted-in payouts, reconciles subscriptions and outstanding transfers, renews domain checks, and expires velocity counters. `compose.production.yml` includes both services. Ensure the maintenance service runs and monitor delivery failures, provider balances, domain expiry and subscription reconciliation.

Before launch, verify real checkout authorization, renewal failures/recovery, cancellation, provider webhook retries, payout processing/reversal, custom-domain TLS, backups/restore and realistic tracking load in staging. Contract tests verify local behavior; live billing lifecycle verification requires configured provider accounts.
