# CI/CD — personal testing on free plans

GitHub Actions runs on pushes, pull requests, and manual dispatch. Both jobs must pass: Python lint/format, PostgreSQL integration tests, Redis tests, dependency vulnerability audit, and a Docker image build. Actions have read-only repository permissions, pinned action revisions, timeouts, and cancel outdated CI runs.

The existing Render Free web service uses `autoDeployTrigger: checksPass` on `main`. Render rebuilds the tested commit after all GitHub checks pass; a failed check prevents automatic release. Startup applies migrations and starts the API, worker, and maintenance process. `/health/ready` gates service health. No paid preview environments or workers are created.

The existing service is connected through the Render GitHub app to this repository. A public repository URL alone cannot receive automatic Git events. Verify a new main-branch commit appears in Render after its Actions jobs turn green. If it does not, reconnect the repository using the service Source settings. The Blueprint must also be synced after changes to render.yaml.

For rollback, deploy a known good commit from Render. Database migrations are not automatically reversed; use backward-compatible migrations. Restore automatic deploys after a manual specific-commit rollback only when main is safe.

Keep this deployment for dummy-data testing. Free PostgreSQL expires November 15, 2026; the web service sleeps when idle. Do not add live billing or payouts to this environment.
