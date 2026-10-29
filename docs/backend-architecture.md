# Backend architecture

The backend is a modular FastAPI application organized by feature. Public URLs,
payloads, authorization rules, database tables and executable entry points are
preserved by this refactor. The frontend does not need an API migration.

```text
app/
  main.py                 Application composition, lifecycle, errors and health
  api/router.py           Explicit API router registration
  core/                   Configuration, database sessions, authentication and shared services
  permissions/            Roles, employee/API scopes and resource ownership guards
  middleware/             Security limits/headers, company selection and domain validation
  persistence/            Shared SQLAlchemy models and control-plane directory models
  shared/schemas.py       Common request contracts used across features
  tenancy/service.py      Company provisioning, routing context and database migrations
  features/
    auth/                 Login, registration and session endpoints
    accounts/             Network settings, API keys, notifications and integrations
    partners/             Partner accounts and their representations
    campaigns/            Campaign routes, schemas, services, policy, configuration and caps
    tracking/             Links, redirects, fraud rules, landing pages and browser tracking
    conversions/          Postback endpoints and conversion processing
    reporting/            Reports, audit history and summaries
    callbacks/            Callback configuration, templates and delivery
    domains/              Domain management and shared domain validity policy
    billing/              Subscriptions, payouts, invoices and payout automation
    tools/                Imports/checks, smart links and campaign automation
    influencer/           Influencer placements and manual results
    sources/              Source blocking and presets
    platform/             Platform administrator operations
    portals/              Workspace and portal endpoints
  worker.py               Existing outbox-worker entry point
  maintenance.py          Existing maintenance entry point
migrations/               Alembic revisions and metadata registration
tests/                    API, isolation, migration and architecture regression tests
scripts/                  Operational commands
```

## Responsibilities and dependencies

- `main.py` assembles the application; `api/router.py` registers feature routers.
  Keep registration order explicit, particularly when static and parameterized
  paths share a prefix.
- Feature routers own HTTP validation and responses. Reusable business operations
  belong in feature services, rather than being imported from another router.
  Split a feature further when it has a distinct responsibility; small features
  can keep their request schemas beside their routes.
- `permissions/` contains resource/tenant ownership checks, campaign management
  guards, role guards, employee capabilities and partner API scope enforcement.
  Authentication dependencies remain in `core/security.py`. Moving a check does
  not replace it: new endpoints must use the existing actor dependency and the
  applicable resource guard.
- Middleware handles request-wide concerns. Company selection is a routing hint;
  authorization still checks the authenticated workspace and resource ownership.
  Domain middleware uses the shared domain policy, without importing a router.
- Shared layers must not import HTTP routers. `tests/test_architecture.py` enforces
  this for core, permissions, middleware, persistence, shared contracts and tenancy.
- SQLAlchemy models remain in a shared persistence package because their foreign
  keys span features. Alembic registers the application and company-directory
  models explicitly. Moving modules does not require a new database revision.

## Adding a feature

Add its HTTP endpoints to `features/<name>/router.py` and register that router in
`api/router.py`. Introduce `schemas.py` or `service.py` when the feature needs them;
do not create empty layers, repository wrappers or dependency-injection containers.
Use explicit `app.*` imports so dependencies remain visible. Update operational
scripts and tests when moving an import; avoid compatibility aliases that leave
two competing module layouts.

Run the PostgreSQL suite for migrations, concurrent postbacks and company isolation:

```powershell
.\.venv\Scripts\python.exe scripts/test_postgres.py
.\.venv\Scripts\python.exe -m ruff check app tests scripts migrations
.\.venv\Scripts\python.exe -m ruff format --check app tests scripts migrations
```

The executable targets remain `app.main:app`, `python -m app.worker` and
`python -m app.maintenance`. The company migration command is now
`from app.tenancy.service import migrate_companies; migrate_companies()`.
