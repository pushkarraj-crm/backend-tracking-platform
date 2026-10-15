# Operations

## Monitoring

Liveness `/health` does not query dependencies. Readiness `/health/ready` requires PostgreSQL, the current migration revision, and configured Redis. Route-based Prometheus counters and latency histograms are available at `/metrics` with `Authorization: Bearer <METRICS_TOKEN>`. This deployment uses one API process per container; aggregate metrics across containers rather than adding unconfigured multiprocess workers.

Log records contain request ID, method, route template, status, and duration. They omit raw URLs, request bodies, credentials, IP addresses, and SQL exception parameters. Uvicorn access logging is disabled by the production command. Configure alerts for 5xx rates, latency, readiness, database connections/storage, Redis errors, pending delivery age, and failed deliveries. Platform owners can see queued delivery counts; network staff can inspect their own delivery statuses.

## Worker delivery guarantees

Start with `python -m app.worker`. Multiple workers claim with `FOR UPDATE SKIP LOCKED`. A 60-second lease protects each job; expired leases are recovered. Network calls are timed out; retries use exponential delay and jitter and stop after eight attempts. Delivery is at least once: a receiver may process a request before a worker crashes, so receivers must deduplicate `X-Event-ID`. Conversion notifications also carry a monotonically increasing version to ignore older status updates.

Webhook signature is HMAC-SHA256 of `timestamp + "." + raw_request_body` using the endpoint secret. Check the timestamp freshness and the HMAC in constant time, then deduplicate the event. Only explicitly allowlisted public HTTPS hosts are eligible for callbacks. Add a legitimate host to `OUTBOUND_ALLOWED_HOSTS` only after reviewing ownership; private/reserved addresses and redirects remain blocked.

## Backups and retention

Enable managed PostgreSQL backups and retain copies according to your requirements. Perform a restore drill into a separate database and verify click/conversion/ledger consistency before relying on backups. Preserve the encryption key separately with access controls: losing it makes endpoint secrets and pending deliveries unreadable. Back up environment/service configuration securely; never commit it.

Establish explicit retention policies for clicks, audit records, consumed action tokens, expired sessions, and delivered/failed jobs before scaling. Do not delete clicks referenced by financial records. Delivered encrypted payloads are cleared. Avoid running arbitrary SQL cleanup on a live tenant database; use reviewed, batched maintenance procedures and backups.

## Key rotation

Rotate JWT with the new secret as `JWT_SECRET` and the previous value as `JWT_PREVIOUS_SECRET`. Remove the previous key after the maximum access-token lifetime. Refresh sessions remain database-controlled. For an emergency, revoke all sessions and remove previous signing keys immediately.

The encryption key cannot simply be replaced: existing encrypted values must first be re-encrypted in a reviewed migration. Advertiser keys rotate per offer; the previous key stops working immediately. Affiliate webhook endpoint changes rotate its signing secret, and pending delivery uses the current endpoint configuration. Razorpay webhook-secret rotation must account for retries signed with an earlier secret, as described in Razorpay's documentation.

## Business accounting

Approval inserts a commission entry. Rejection after approval inserts an equal reversal. Repeated identical approvals do not insert duplicates. Completed payouts are recorded with a unique bank/provider reference and cannot exceed the current commission balance. If a paid conversion is later reversed, the balance can become negative and subsequent commission offsets it. Automated RazorpayX payouts reserve available commissions before submission and use each network's own account. Provider reversals create compensating ledger credits. See [advanced features](advanced-features.md) for configuration and reconciliation rules.

Run `python -m app.maintenance` alongside the worker. It schedules opted-in payouts, reconciles subscription and payout states, renews domain ownership checks, and expires velocity counters. Alert on maintenance failures and unresolved payout reservations. Never release a reservation merely because a provider request timed out; reconcile the stable provider request first.

Default trial quotas are 10 users and 25 offers; starter/business quotas are configurable in `PLAN_LIMITS`. Resource creation locks the workspace to prevent parallel quota bypass. Trial expiry uses server time and is enforced on business/tracking operations, while workspace/billing endpoints remain available for renewal.
