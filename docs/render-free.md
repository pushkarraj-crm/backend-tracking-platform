# Personal testing on Render Free

`render.yaml` explicitly requests one Free web service, one Free PostgreSQL
database and one Free Key Value instance. It creates no paid workers or disks.
Do not apply this Blueprint to existing production data.

The `staging` environment retains PostgreSQL, Redis, random signing/encryption
secrets, HTTPS-only URLs, Secure cookies, exact frontend origins, host checks and
the existing tenant/role authorization. This personal preview allows dummy-email
signup without email verification; it is not a production configuration. SMTP is
unavailable until separately configured, so email actions report an explicit
unavailable response. Only Razorpay/RazorpayX test keys are accepted. No provider
keys are supplied by the Blueprint and no real bank transfers are authorized.

The startup supervisor normalizes Render's database URL, applies migrations and
runs the API, outbox worker and maintenance process together. All stop when the
Free web service sleeps or restarts. This is not an always-on worker deployment;
it does not make synthetic requests to keep the service awake.

Set `FRONTEND_BASE_URL` to the canonical Vercel frontend HTTPS URL when applying
the Blueprint. The API's public URL and host allowlist are derived from Render's
`RENDER_EXTERNAL_URL`. Do not replace generated secrets on redeploy: existing
encrypted outbox/provider data depends on the retained encryption key.

On Vercel, set the frontend project's server-only `API_ORIGIN` to this Render
service's HTTPS origin. Its API proxy preserves HttpOnly refresh cookies and
rewrites `/auth` cookies to `/api/auth`, avoiding third-party-cookie dependence.
Use the canonical frontend URL for login; preview aliases are intentionally not
added to the API's trusted origins automatically.

Render Free sleeps after idle periods, shares a limited monthly hour allowance
across the account, blocks standard SMTP ports, and its PostgreSQL database
expires after 30 days. Export test data before expiry. Do not enter real customer
data or payment credentials. Check the account's included usage and spending
settings; plan upgrades or usage purchases are not authorized by this setup.

References: https://render.com/docs/free and https://vercel.com/docs/plans/hobby.
