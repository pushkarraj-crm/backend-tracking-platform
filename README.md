# Affiliate network API

Backend for a multi-tenant affiliate marketing SaaS deployed on DigitalOcean. The frontend is a separate application. PostgreSQL stores business data; Redis provides shared rate limits; a durable PostgreSQL outbox processes email, billing reconciliation, and affiliate postbacks.

See [backend architecture](docs/backend-architecture.md) for the feature packages,
router registration, permissions, middleware and dependency boundaries.

## Local operation

The local PostgreSQL instance is already initialized on `127.0.0.1:5433`. Credentials are in the ignored `.env`. On later sessions:

```powershell
.\.venv\Scripts\python.exe scripts/dev_postgres.py start
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --no-access-log
```

Run the worker in another terminal:

```powershell
.\.venv\Scripts\python.exe -m app.worker
.\.venv\Scripts\python.exe -m app.maintenance
```

Interactive development API documentation: http://localhost:8000/docs. JSON schema: http://localhost:8000/openapi.json. Production disables both public documentation endpoints. `GET /health` is liveness; `/health/ready` verifies the migration revision, PostgreSQL, and configured Redis.

For a fresh Windows checkout, create `.venv`, install `requirements-lock.txt`, and run `scripts/dev_postgres.py init` once. The initializer uses installed PostgreSQL 16 binaries, creates `affiliate_platform` and `affiliate_test`, and generates local passwords, JWT, encryption, and metrics secrets. `--bin` selects another PostgreSQL installation. `stop` and `status` manage only the project instance. On Linux or with another server, supply `.env` and run Alembic directly.

## Implemented API

| Area | Endpoints and behavior |
| --- | --- |
| Signup | `GET /auth/registration-options`, `POST /auth/register`: contact/company profile, consent record, 15-day trial, no card or setup fee |
| Authentication | Email/password login, email verification/resend, password reset, rotating refresh cookies, logout/all sessions |
| People | Admin-created roles; affiliates can create and manage their own publishers; publishers have separate logins |
| Offers | Staff-managed offers; advertiser submissions wait for approval; assignment controls affiliate/publisher access |
| Tracking | Durable click IDs, placement `sub_id`, redirects, configurable attribution windows |
| Conversions | Header-authenticated postbacks, atomic duplicate protection, staff approval/rejection, versioned notifications |
| Reporting | SQL aggregation; separate currency totals; advertiser, affiliate, and publisher visibility |
| Campaigns and invoices | Advertiser CPL/CPS campaigns, publisher applications, direct invoices, external payment reporting and publisher confirmation |
| SaaS billing | Razorpay subscription checkout, signed/deduplicated webhooks, provider-state reconciliation, cancellation |
| Platform owner | Workspace list/summary, suspension/reactivation, restricted platform-owner controls |
| Operations | Audit log, delivery queue/retries, dependency readiness, bearer-protected Prometheus metrics |

See [advanced features](docs/advanced-features.md), [frontend API integration](docs/frontend-api.md), [DigitalOcean deployment](docs/digitalocean.md), and [operations](docs/operations.md).

## Security and data guarantees

- Tenant identity is derived from a signed token. Composite foreign keys prevent cross-workspace relationships. Public tracking and postbacks resolve ownership from the link/offer, never from an arbitrary tenant parameter.
- Access tokens expire and are tied to a revocable database session. Refresh tokens rotate; reuse revokes that session. Password resets and deactivation revoke existing sessions. JWT signing supports a previous key during controlled rotation.
- Passwords use Argon2 with bounded concurrent hashing to limit memory exhaustion. Outbound signing keys and queued payloads are encrypted. No tokens/passwords are placed in request logs.
- Browser refresh/logout require a CSRF header and matching cookie, with allowed-origin checks. Production cookies are Secure and HttpOnly where appropriate. CORS accepts explicit origins and credentials.
- Production requires PostgreSQL, Redis, HTTPS, verified email/SMTP, encryption, metrics authentication, Razorpay configuration, and explicit hosts. Public signup additionally requires Terms and Privacy URLs. The production example keeps signup disabled until those documents exist.
- Request bodies are bounded by both the application and gateway. Rate limits are distributed and fail closed on Redis errors. Database connections have limits, health checks, UTC timestamps, statement timeouts, and pooling.
- Monetary API values are decimal strings. Conversion duplicates cannot double-count a commission. Approval and invoice creation lock commission records; reversals remain visible instead of deleting earned entries. Invoice reservations prevent double billing. Advertisers pay publishers externally; platform fund transfers are retired.
- Outbound callbacks require explicitly allowed HTTPS hosts resolving only to public addresses. The delivery connection pins the validated IP and verifies TLS for the hostname. Redirects are not followed. Events have stable IDs and conversion versions for recipient deduplication/order handling.
- Outbox processing uses leases and PostgreSQL `SKIP LOCKED`, bounded retries, failed-delivery visibility, and lease recovery. Delivery is **at least once**; recipients must deduplicate. Delivered payloads are cleared.

## Quality checks

```powershell
.\.venv\Scripts\python.exe scripts/test_postgres.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests scripts migrations
.\.venv\Scripts\python.exe -m ruff format --check app tests scripts migrations
.\.venv\Scripts\python.exe -m alembic check
.\.venv\Scripts\python.exe -m pip_audit --disable-pip --no-deps -r requirements-runtime-lock.txt
```

PostgreSQL tests use a unique disposable schema in `affiliate_test`. Fast tests use SQLite in test mode only. Migrations are checked for upgrade/downgrade consistency. CI runs PostgreSQL tests, lint/format checks, a dependency audit, and container build. Runtime and development dependency versions are locked separately; production installation verifies published artifact hashes. Regenerate locks with `scripts/lock_dependencies.py`, then `scripts/hash_runtime_lock.py`, and rerun the checks.

Measure staging traffic with `scripts/load_test.py --url <staging-tracking-url> --requests 1000 --concurrency 20`. Tracking tests record real clicks, so use a dedicated staging offer. Capacity depends on the DigitalOcean machine/database and must be measured; no throughput guarantee is inferred from unit tests.

## Scope and release validation

This repository contains the backend only, including targeting, caps, rule-based fraud detection, advertiser landing/tracking domains, direct publisher invoices, reporting/export APIs, and subscription lifecycle reconciliation. Live DigitalOcean deployment, DNS/TLS issuance, SMTP delivery, Razorpay subscription authorization/renewals, and backups/restore still require verification with your actual service configuration before admitting paying customers. Contract tests do not prove live provider integration or production capacity.

See [campaigns, invoices, advertiser domains and S2S integration](docs/campaigns-invoices-postbacks.md) for the current product workflow and ad-URL configuration limits.


## Network operations extension

See [network operations API](docs/network-operations-api.md), [provider and S2S setup](docs/network-integrations.md), and [Offer18 reference review](docs/offer18-feature-review.md). Provider settings are configuration drafts; live provider adapters remain disabled.
