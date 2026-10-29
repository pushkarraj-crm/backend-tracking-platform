# Security fixes — 29 October 2026

## Changes

- Every tracking request evaluates source blocks and click rejection rules before
  reusing a previous click. An allowed duplicate still reuses the original click
  without incrementing click caps or replacing its attribution attributes.
- API metrics map unknown HTTP methods to `OTHER`, bounding their method labels.
- Restricted staff can read `/targets` with `campaigns:read`; source-block reads
  require `fraud:read` and mutations require `fraud:review`. Missing permissions
  still return 403. No new administrative privileges are granted.
- The development lock upgrades filelock to 4.2.1. CI audits both lockfiles.
- The Vercel proxy signs the visitor identity; the backend verifies its signature
  before IP throttling, geolocation, fraud checks and attribution.

## Configure proxy identity before deployment

Set **the same random `PROXY_IP_SECRET`** (at least 32 characters) in:

1. The backend hosting environment (Render for the existing preview).
2. The frontend's server environment (Vercel for the existing preview).

Generate it using a password manager or `secrets.token_urlsafe(48)`. Store it as a
secret, never in source control or a `VITE_*` variable. Configure both services,
then deploy the backend before deploying the frontend. The frontend fails closed
with 503 if the secret or provider visitor identity is missing. There is no fallback
that silently identifies everyone as the proxy. Direct backend clients still work
without forwarding headers. No database migration is required.

On Vercel, `api/proxy.js` uses the platform-overwritten `x-forwarded-for` header.
Outside Vercel it uses the actual socket peer and ignores forwarded-for headers.
Deploying this handler behind another proxy requires a separately verified trust
configuration; it must not accept a public header as proof of a visitor's IP.

The signed metadata has a 60-second clock tolerance and binds the IP, timestamp,
HTTP method, raw path/query and company slug using HMAC-SHA256. Duplicate identity
headers, malformed IPs, stale signatures and request/tenant mismatches are rejected.
The backend ignores unsigned forwarded-IP headers at the application layer; keep
Uvicorn's platform proxy allowlist narrow and the ingress responsible for sanitizing
its transport headers. The signature authenticates forwarding metadata, not API
authorization. Existing bearer tokens, CSRF and resource permissions still apply.

Rotate the shared secret by updating both environments in a coordinated deployment;
in-flight requests signed with an old key will fail. Keep host clocks synchronized.

Provider reference: https://vercel.com/docs/headers/request-headers

## Verification

The new regressions cover both rejection policies during duplicate-click reuse,
bounded metric labels, allowed/denied staff operations, unsigned/forged/expired
proxy identities, request/tenant tampering and independent visitor rate limits.
Frontend and backend share a fixed signature vector to verify protocol compatibility.
The frontend proxy checks run in CI through `npm test` alongside the existing suite.

Validation completed:

- Full PostgreSQL suite: 110 passed, one optional test skipped.
- Frontend suite: 62 passed; lint and production build passed.
- Both backend lockfile audits and the frontend audit: no known vulnerabilities reported.
- Backend dependency compatibility, lint and formatting checks passed.

These results validate the code locally. The shared hosting secret and coordinated
deployment are still required to enable the proxy fix on a live installation.
