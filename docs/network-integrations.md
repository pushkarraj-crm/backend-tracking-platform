# Network integration setup

This release provides **configuration drafts, not active provider adapters**. The
API always reports `active: false`. Supplying credentials does not enable uploads,
send messages, provision storage or create a provider connection. No credentials
are needed to use the core campaign, tracking, invoice and report workflows.

## Account configuration

Open **Network settings → Integrations**, choose a provider and save its public
identifiers. Alternatively, authenticated `GET /integrations/catalog` returns the
supported field names and references. Use `PUT /integrations/{provider}`:

```json
{"configuration":{"dataset_id":"YOUR_TEST_DATASET"}}
```

The configuration belongs to the signed-in account within its network. An admin
cannot read another account's secrets. Optional `secrets` are encrypted with the
server encryption key; responses show field presence only. Omitting `secrets`
keeps existing credentials; providing it replaces the entire credential set;
`secrets: {}` clears it. Delete a draft with `DELETE /integrations/{provider}`.
Never put credentials in landing URLs, browser code, CSV imports or screenshots.

| Provider | Public configuration | Credential preparation | Before a live adapter is built |
|---|---|---|---|
| Meta | Dataset ID, test event code | Access token scoped to the test asset | Verify asset ownership, event mapping, consent, event-ID deduplication and Test Events |
| Google | Customer/action IDs, OAuth client ID, manager account, Cloud project number, ingestion method | OAuth client secret/refresh token; legacy developer token only where applicable | Determine Data Manager eligibility and current ingestion API; validate consent, click IDs and duplicate order handling |
| AppsFlyer | App ID, media source | API token and click-signing key if required | Agree the install/in-app event mapping and callback authentication |
| Adjust | App token | API token | Agree event tokens, attribution window and callback authentication |
| Branch / Singular / Kochava | App ID | Provider API key | Agree event names, click identifiers, deduplication and retry policy |
| TikTok / Snapchat | Pixel ID | Access token | Validate test events, consent, deduplication and supported event schema |
| Slack | Channel label | Incoming webhook URL | Verify channel ownership and choose notification types; draft saving sends nothing |
| Telegram | Chat ID | Bot token | Verify bot/chat ownership and choose notification types; draft saving sends nothing |
| SMTP | Host, port, username, from address | Password | Confirm authenticated TLS, sender ownership, bounce handling and quotas |
| Object storage | Endpoint, bucket, region, public asset base | Restricted access key and secret | Configure bucket policy, signed uploads, file limits, scanning and asset delivery |

Google's current documentation states that new offline-upload integrations face
an access restriction on the older UploadClickConversions API and directs them
to Data Manager. Do not select a legacy implementation from the field names alone.
Read the [current Google ingestion guidance](https://developers.google.com/google-ads/api/docs/conversions/upload-offline)
before implementing the adapter. The catalog deliberately does not activate either
method. [Meta's reference](https://developers.facebook.com/docs/marketing-api/conversions-api/)
is also linked in the setup screen; provider requirements must be checked again
when actual credentials and account eligibility are available.

## Core advertiser-to-platform S2S connection

1. Create a campaign, approve the publisher and generate its link.
2. Preserve the platform `click_id` from the landing query or consent-gated capture.
3. Keep the advertiser postback key on the advertiser's server.
4. Send `POST /postbacks/{campaign_id}` with `X-Postback-Key` and the payload below.
   Company-mode public URLs use `/c/{company_slug}/postbacks/{campaign_id}`.
5. A successful new conversion returns 201; an identical retry returns 200 and
   the same conversion ID. Conflicting reuse returns 409. Never generate a new
   transaction ID merely to retry a failed network request.

```json
{
  "click_id":"PLATFORM_CLICK_ID",
  "transaction_id":"ADVERTISER_ORDER_ID",
  "event_key":"qualified_lead",
  "advertiser_parameters":{"adv_sub1":"non-personal-reference"}
}
```

`event_key` defaults to `initial`. Create named events first. CPL accepts leads;
CPS requires `event_type: "sale"`, a positive decimal-string `sale_amount` and the
campaign currency. Repeatable events require a distinct transaction per actual
conversion. Commission rules are evaluated from stored click attributes and the
authenticated event, then preserved in the conversion pricing snapshot. Postback
IP restrictions are optional and use the server-observed client IP, not a supplied
header. Ensure the trusted reverse proxy is configured correctly before enabling.

## Platform-to-publisher callbacks

Use **Publisher callbacks** to configure a global endpoint or campaign override.
Endpoints require HTTPS 443 and a deployment operator-approved
`OUTBOUND_ALLOWED_HOSTS` entry. All resolved addresses must be public; delivery
pins the checked address, validates hostname TLS and does not follow redirects.
Draft integration provider settings do not change this allowlist.

Saving a callback rotates the signing secret, shown once. URLs are encrypted;
common credential query fields are redacted in reads. Macros are allowed only in
query values. A campaign override takes precedence over the global callback.

POST signatures use HMAC-SHA256 over `timestamp + "." + raw_JSON_bytes`
(`X-Webhook-Signature-Version: 1`). GET signatures use
`timestamp + ".GET\n" + exact_request_path_and_query` (version 2). Read the
`X-Webhook-Timestamp`, `X-Webhook-Signature`, `X-Event-ID` headers; compare signatures
in constant time, reject timestamps outside your tolerance and deduplicate event
IDs. Preserve the request's exact encoded query when validating GET signatures.
The signed GET includes `relay_event_id`, `relay_event_type` and `relay_version`.

Delivery is at least once. Status updates can arrive after an earlier update;
use conversion version numbers to reject stale state. Retries use the same durable
event ID. Failed deliveries can be retried from the log; test callbacks use zero
payout and TEST_ONLY identifiers and create no financial records. A test can reach
the external endpoint, so use a controlled test receiver.

## Advertiser website tracking

Redirect links are `/t/{publisher_link_id}`. Forwarded metadata includes source,
aff_sub1–10, aff_click_id, sub_aff_id, UTM and advertising click identifiers.
Deep links require explicit campaign opt-in and exact permitted advertiser hosts.
Query macros are URL encoded and cannot change the destination host or path.
Smart links rotate approved publisher links for one advertiser and enforce the
selected campaign's targeting, fraud and caps. They never cloak reviewer content.

For a stable advertiser final URL, install `/sdk/relay.js` on the advertiser's
HTTPS page and call `RelayTracking.capture({captureUrl, consent:true})` **after
the site's consent decision**. Its capture URL is `/capture/{link_id}` (with the
company prefix where applicable). The endpoint requires a matching Origin and
configured advertiser destination or currently verified landing hostname. The
helper preserves an existing platform click ID and does not set a tracking cookie.
Persist the returned ID with the advertiser's lead/order for its authenticated
server postback. Never place the postback key in browser JavaScript.

Impression URLs `/i/{link_id}` return a no-cache GIF and record impressions
separately from clicks. No public paid-conversion pixel or probabilistic device
fingerprinting is provided. Advertising platform approval depends on the actual
domain, destination, content and provider review; link health checks cannot certify it.

## Production prerequisites

Back up PostgreSQL and the encryption key before migrations. Run migrations in
the control database and every company database. Start API, delivery workers and
maintenance; automation rules are evaluated hourly when maintenance is running.
Free hosting sleep pauses background processing and is suitable for testing.
Company DB provisioning, DNS/TLS, billing provider tests, outbound host approval,
provider-specific adapters and representative load testing remain separate setup
and verification work. Advertisers continue to pay publishers externally through
invoices; none of these integrations initiates publisher payments.
