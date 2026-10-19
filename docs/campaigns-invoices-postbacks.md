# Network campaigns, advertiser domains, invoices and S2S

Each company remains an isolated network. Company mode uses its existing database
provisioning and hostname directory; this change does not create a shared marketplace.
Advertisers and publishers belong to a network. Publishers may work directly with
advertisers or have an optional parent affiliate. Affiliate-created publishers still
belong to that affiliate. Login continues to use email and password.

## API workflow

1. Network admin creates advertiser and publisher accounts with `POST /users`.
2. Advertiser creates `POST /campaigns` with `name`, HTTPS `destination_url`,
   `campaign_model` (`CPL` or `CPS`), `payout`, `revenue`, `currency` and optional
   `description`. Admin/staff supply `advertiser_id`; advertisers can own only their
   campaigns. CPS requires `sale_percentage` greater than zero and at most 100.
   Amounts are decimal strings. CPL uses the fixed payout; CPS computes commission
   from the accepted sale amount and percentage, rounded down to four decimals.
3. Publishers discover `GET /campaigns` and apply through
   `POST /campaigns/{id}/apply` with an optional message.
4. `GET /campaign-applications` is scoped to the caller. The owning advertiser or
   network staff review using `PATCH /campaign-applications/{id}` with
   `status: approved|rejected` and optional `note`. Rejection revokes future traffic.
5. Approved publishers call `POST /campaigns/{id}/link`. Optional `domain_id` selects
   a verified tracking domain of that advertiser. Available domains are returned by
   `GET /campaigns/{id}/tracking-domains`. Tracking validates application approval,
   campaign/account status, entitlement, targeting, fraud rules and caps.
6. Advertiser sends the S2S conversion below. Advertiser or network staff approve
   or reject via `PATCH /conversions/{id}`. Approval snapshots publisher earnings.

Legacy offers remain available through the existing API. They are not automatically
invoice-eligible, preventing old already-settled earnings from being invoiced again.

## Advertiser → platform postback

Use the campaign's returned `postback_url` exactly. In company mode it includes
`/c/{company_slug}/postbacks/{campaign_id}`. Persist the tracking redirect's `click_id`
with the lead/order on the advertiser's server, then POST from that server:

```http
POST /postbacks/CAMPAIGN_ID
Content-Type: application/json
X-Postback-Key: CAMPAIGN_SECRET

{"click_id":"32_HEX_CLICK_ID","transaction_id":"unique-lead-123","event_type":"lead"}
```

For CPS:

```json
{"click_id":"32_HEX_CLICK_ID","transaction_id":"unique-sale-123","event_type":"sale","sale_amount":"100.00","sale_currency":"USD"}
```

The key is returned once on campaign creation or
`POST /campaigns/{id}/postback-key`; rotating invalidates the previous key.
Never embed it in ads, browser JavaScript, query strings or public tracking links.
The caller cannot supply the publisher commission. Sale currency must match the
campaign. New conversions return 201; identical retries return 200 with the original
ID; conflicting transactions/clicks/payloads return 409. One conversion per click
is currently supported. New conversions start pending. Signed publisher updates
are queued transactionally on approval/rejection, including subsequent reversals.

## Platform → publisher postback

Publishers use `GET /publisher-postback` and `PUT /publisher-postback` with
`{"url":"https://publisher.example/conversions"}`. Each save rotates the signing
secret and returns it once. The operator must enable the hostname in
`OUTBOUND_ALLOWED_HOSTS`. HTTPS port 443, public-only DNS resolution, pinned network
connections, verified TLS certificates and no redirects protect outbound delivery.

POST body: `{"id":"EVENT_ID","type":"conversion.updated","data":{...}}`.
Data includes conversion/campaign/click/transaction IDs, status, payout, currency,
event type, sale amount and a monotonically increasing conversion version.

Verify `X-Webhook-Signature` using HMAC-SHA256 over
`X-Webhook-Timestamp + "." + raw_body` with the returned secret. Compare in constant
time, enforce a timestamp tolerance, deduplicate `X-Event-ID` durably, and ignore
older versions. Persist before returning 2xx. Retries keep the event ID, use a fresh
signed timestamp and back off up to eight attempts. Network staff can inspect
`GET /deliveries`; admin retries failed events with `POST /deliveries/{id}/retry`.
Delivery is at least once; recipients must be idempotent. Workers must be running.

## Publisher invoices and external payment

`GET /publisher-invoices/eligible-conversions` returns approved, uninvoiced new
campaign conversions for the caller. Publishers submit
`POST /publisher-invoices` with `number` and `conversion_ids` (1–500 unique IDs).
One invoice must contain one advertiser and one currency. Conversion reservations,
database locks and unique constraints prevent concurrent duplicate invoicing.
Repeating an invoice number with the identical set returns the original invoice.

`GET /publisher-invoices`, `/summary`, and `/{id}` are party/network scoped.
The detail endpoint includes immutable commission lines and an append-only history.
`POST /publisher-invoices/{id}/actions` accepts:

| Party | Action | Transition |
|---|---|---|
| Advertiser | approve | submitted → approved |
| Advertiser | reject, with note | submitted → rejected |
| Publisher | cancel | submitted → cancelled |
| Advertiser | report_payment, with payment_reference | approved/disputed → payment_reported |
| Publisher | dispute, with note | payment_reported → disputed |
| Publisher | confirm_receipt | payment_reported → paid |

Payment reporting accepts an optional HTTPS `receipt_url` and note. Every report's
reference/proof is retained in its history even after a new report. Rejection or
cancellation releases invoice reservations; invoiced conversions cannot silently
be reversed. Only the publisher can confirm receipt. Network staff inspect but
cannot impersonate either payment party. Repeated invalid transitions return 409.

The platform never transfers publisher funds. Old `/automated-payouts/*` routes
and manual `POST /payouts` return 410. Old queued payout jobs fail without contacting
a provider, and maintenance no longer schedules transfers. Legacy financial tables
and read-only payout/balance APIs remain for legacy reconciliation. New campaign earnings are excluded from those legacy balances; use invoice summaries for current payable amounts. Existing pending
provider transfers, if any, require external reconciliation; no success is invented
and no historic reservations are silently released. Razorpay **SaaS subscription**
billing, its 15-day trial and lifecycle maintenance remain separate.

## Advertiser domains and landing pages

`POST /tracking-domains` accepts `hostname`, `purpose: tracking|landing|both`, and
an advertiser ID for admin-created advertiser domains. Advertisers can register only
their own. Create the returned TXT/CNAME records, verify with
`POST /tracking-domains/{id}/verify`, and keep records installed. List and disable
are owner/network scoped. DNS ownership expires after seven days and maintenance
revalidates; revoked domains cannot serve traffic or obtain certificates.

`POST /tracking-domains/{id}/verify-https` checks the actual TLS certificate over a
pinned public connection. HTTPS status is a dated observation, valid in the list for
24 hours, and separate from DNS verification. Deploy the configured Caddy gateway
with its protected certificate permission hook (or configure an equivalent ingress).
The application cannot issue a working certificate or modify external DNS merely
because a database row is verified. No real advertiser domain is configured yet.

`POST /landing-pages` accepts `offer_id`, verified `domain_id`, URL-safe `slug`,
`title`, plain-text `body` and `button_text`. Domain and campaign must share the
same advertiser. List and activate/pause with `GET /landing-pages` and
`PATCH /landing-pages/{id}`. Public `https://offers.brand.com/l/summer` returns an
escaped HTML page without arbitrary scripts or HTML. Add `?link=TRACKING_LINK_ID`
for that campaign to attribute the CTA: the page itself is visible to crawlers and
visitors; clicking the CTA records a click and passes `click_id` to the advertiser.
Viewing a landing page alone is not recorded as an affiliate click. Custom hosts
serve public landing/tracking paths, not account APIs.

## Meta and Google ad URLs

Use a stable HTTPS landing URL on the advertiser's domain, with the same substantive
content and destination for reviewers and visitors. The tracker preserves bounded
`gclid`, `gbraid`, `wbraid`, `fbclid`, UTM parameters and `sub_id` across the CTA and
redirect. Destination URLs are configured by the advertiser; query strings cannot
choose a redirect destination. The redirect adds one platform `click_id`.

Google final URLs and tracking templates are different fields. With parallel
tracking the visitor opens the final URL directly; a click ID generated only in a
background redirect will not reach that visitor. Use the hosted attributed CTA
flow or implement advertiser-side click capture before relying on parallel tracking.
This implementation does not claim Google click-tracker certification. Custom domains
do not guarantee approval or repair poor domain reputation, prohibited content,
destination mismatch or account restrictions. Diagnose the actual rejection when
available; never cloak content or change domains to evade enforcement.

Primary reference: [Google final URLs and tracking templates](https://support.google.com/google-ads/answer/6273460).

## Verification and rollout

Run PostgreSQL migration roundtrip and API tests, including tenant/party isolation,
S2S replay/conflict checks, invoice lifecycle and retired transfer tests. Run frontend
`npm run verify`. Deploy backend migration/API before the frontend release. Existing
company-mode production prerequisites (domain, database provisioner, TLS gateway)
remain required; personal free-tier staging stays in its existing shared mode until
those are supplied. No paid infrastructure is provisioned by this change.
