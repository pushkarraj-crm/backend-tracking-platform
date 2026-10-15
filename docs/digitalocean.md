# DigitalOcean deployment

Deploy the API, worker, Redis, and Caddy gateway on a Linux Droplet. Use DigitalOcean Managed PostgreSQL in the same region/VPC with a restricted application role. A frontend can use a separate host under the same parent domain.

## Configure infrastructure

1. Provision a Droplet with Docker/Compose installed and a managed PostgreSQL database. Size it using staging load results, not a presumed traffic rate.
2. Restrict SSH to your administrator IPs. Allow public HTTP/HTTPS to the gateway. Do not expose Redis or API port 8000. Add the Droplet/VPC to the database's trusted sources.
3. Point the API DNS hostname to the Droplet. Download the database CA certificate to `deploy/certs/ca.crt`. Use the supplied database host/port and `sslmode=verify-full` in its connection URL. See [DigitalOcean PostgreSQL connections](https://docs.digitalocean.com/products/databases/postgresql/how-to/connect/).
4. Copy `.env.production.example` to `.env.production`, protect its filesystem permissions, and replace all placeholders. Generate distinct JWT, encryption, Redis, metrics, and webhook secrets. Configure SMTP and Razorpay test-mode credentials before live credentials.
5. Set `FRONTEND_BASE_URL` and `CORS_ORIGINS` to your separate frontend. Keep `ALLOW_REGISTRATION=false` until real Terms/Privacy URLs are supplied. Set the policy version when publishing changes.
6. Install your licensed Country MMDB at `deploy/geoip/GeoLite2-Country.mmdb`, matching `GEOIP_DATABASE_PATH`, and arrange database updates. If geotargeting is not enabled, leave that setting empty; country-targeted offers reject unknown locations. Set `TRACKING_CNAME` and `DOMAIN_CERTIFICATE_TOKEN` for custom domains. Configure each network's RazorpayX account through its admin API; see [advanced features](advanced-features.md).

## Migrate and launch

```bash
docker compose --env-file .env.production -f compose.production.yml build
docker compose --env-file .env.production -f compose.production.yml run --rm migrate
docker compose --env-file .env.production -f compose.production.yml up -d redis api worker maintenance gateway
docker compose --env-file .env.production -f compose.production.yml exec api python scripts/create_owner.py
```

Use a dedicated migration credential in the `migrate` service before the migration command, rather than granting the runtime user schema ownership. The runtime user needs SELECT/INSERT/UPDATE on the application tables and no schema creation or superuser privileges. Restrict commission entries, payouts, and audit events to SELECT/INSERT for the runtime user because they are append-only through the API. Provider reconciliation and delivery updates require UPDATE on their own tables. Keep the schema's `alembic_version` readable for readiness.

The image runs as a non-root user on a read-only filesystem. The gateway is the only published service and uses automatic HTTPS. A dedicated Docker subnet assigns the gateway `172.30.0.2`; Uvicorn trusts forwarded headers only from that address. If you change the subnet, update the trusted proxy address too. Caddy enforces body/read timeouts and a 64KB body limit. The application limits bodies independently.

Create the owner interactively and sign in using its email and password. Use the separate frontend's owner login once integrated. The API's interactive docs are disabled in production; development OpenAPI describes all endpoints.

## Razorpay setup

Create recurring plans in Razorpay and map local plan names to their provider IDs in `RAZORPAY_PLANS`. Configure `/billing/webhook` for subscription events and use an independent webhook secret. The backend validates the HMAC-SHA256 signature over raw bytes and deduplicates by event ID, following [Razorpay webhook validation](https://razorpay.com/docs/webhooks/validate-test/). Test checkout, authorization, activation, renewals, failed payments, cancellation, duplicate callbacks, and delayed callbacks in test mode before switching to live keys. See [subscription creation](https://razorpay.com/docs/api/payments/subscriptions/create-subscription/) for plan/cycle settings.

The worker fetches the provider's current subscription state, so out-of-order callbacks do not restore cancelled access. A 15-day trial starts at account creation. Signup requires no card; choosing a subscription during the trial schedules its first recurring cycle for trial expiry. Razorpay's authorization flow and account eligibility must be verified with your merchant account.

## Release process

Require CI success. Tag the image/release, snapshot/backup PostgreSQL, apply reviewed migrations, deploy API and worker, verify readiness, then check a staging click/conversion/billing flow. Choose backward-compatible migrations when performing rolling releases. Downgrading data-bearing migrations is destructive; restore a tested backup when rolling back business data.

Compose is a single-Droplet deployment. Managed database redundancy does not make the API highly available. For multiple API hosts, use an appropriate load balancer, shared Redis and PostgreSQL, and update the trusted proxy configuration. Do not infer capacity or availability guarantees from the supplied Compose file.
