# Company dashboards and PostgreSQL databases

This mode gives each newly registered company a database, PostgreSQL login role,
and dashboard hostname. Its admins, staff, affiliates, advertisers, and publishers
share that company's database. Individual partner accounts do not create databases.

`Media Kendra` reserves `media`; a later `Media House` reserves `media-2`.
Reserved names such as `api` are prefixed with `company-`. Names are normalized to
DNS-safe ASCII labels. The directory's primary key and PostgreSQL advisory lock
serialize reservations across API instances.

## Local development

The project-local PostgreSQL server must be running (`scripts/dev_postgres.py`).
From Backend:

```powershell
.\.venv\Scripts\python.exe scripts/company_dev.py serve
```

This creates/reuses **affiliate_company_dev**, a separate control database. It
does not move or modify accounts in the original affiliate_platform database.
The ignored `.company-dev-key` must be preserved to decrypt development database
credentials. The helper uses the existing local PostgreSQL admin credential in
memory and starts API, delivery worker, and maintenance processes together.

From Frontend, in a separate PowerShell terminal:

```powershell
$env:API_PROXY_TARGET = 'http://127.0.0.1:8001'
npm run dev -- --port 3001
```

Open `http://localhost:3001/register`. Signup returns a company login URL such as
`http://media.localhost:3001/login`. Sign in there with email and password. Local
development cookies use HTTP; deployed staging/production cookies require HTTPS.

## Production configuration (domain can be chosen later)

- Keep COMPANY_DATABASES=false until the domain, database privileges, backups,
  and existing-account migration have been prepared. Current hosted test mode
  stays compatible with existing data while this is disabled.
- Set COMPANY_BASE_DOMAIN to the real base domain, without scheme or wildcard.
  Set FRONTEND_BASE_URL to its public registration origin, normally the apex.
- Add the apex and wildcard `*.yourdomain.com` to the existing frontend project.
  Configure wildcard DNS and HTTPS with the hosting provider. An arbitrary
  subdomain under the existing project.vercel.app hostname is not available.
- Set COMPANY_BASE_DOMAIN on the frontend Vercel project as a **server-only**
  environment variable too. Its API proxy derives X-Company-Slug from Host and
  discards client-supplied routing headers.
- Configure COMPANY_DATABASE_ADMIN_URL with a dedicated PostgreSQL 16+ provisioner
  having CREATEDB/CREATEROLE and permission to set roles it creates. Keep that
  credential separate from the control database's ordinary application login.
  Do not grant tenant roles superuser, CREATEDB or CREATEROLE.
- Preserve ENCRYPTION_KEY: the directory encrypts each company's connection URL.
  Losing it prevents access to all tenant databases. Never put credentials in
  API responses, frontend build variables, Git, or logs.
- Enable COMPANY_DATABASES only after these prerequisites are verified.

Existing shared-database accounts are **not migrated automatically**. Back up
and migrate their data with preserved relationships and a planned cutover before
enabling this mode for an existing installation. Toggling the flag does not move
legacy accounts into the new company databases.

## API changes

- GET /auth/registration-options returns company_databases and company_base_domain.
- POST /auth/register in company mode returns company_slug, dashboard_url,
  login_required=true, and trial_days=15. It does not mint root-domain session
  cookies or put access tokens into redirect URLs.
- Tenant API requests select a company through its host or X-Company-Slug. This
  header is routing information, not authorization. Tokens are bound to both
  company and workspace; all normal role and ownership checks still apply.
- Root registration and company login continue to use email/password only.
  Root /auth/login is for control-plane accounts; new company users log in at
  their assigned company URL. Root operational tenant routes fail closed.
- Generated tracking URLs use /c/{slug}/t/{link_id}; offer responses include a
  postback_url using /c/{slug}/postbacks/{offer_id}. Do not remove the prefix.
- Razorpay subscriptions include company_slug in signed subscription notes.
  The global billing webhook validates the signature before routing to a company.
- Platform-owner workspace listing, summary and suspension read the tenant
  databases through the control directory. Only platform_admin can use them.

## Lifecycle and operations

Provisioning reserves the slug, creates a private database and login, migrates it,
and creates its first network admin before marking the directory entry active.
Tenant roles cannot connect to sibling company databases. Failed/stale provisioning
entries fail closed and keep their names reserved. They require operator inspection;
the service never deletes an existing database automatically or pretends setup
succeeded. Do not retry failed signup repeatedly without investigating its entry.

Render startup migrates the control database and every active tenant database
before serving traffic. Other deployments should run Alembic upgrade head, then:

```shell
python -c "from app.tenancy import migrate_companies; migrate_companies()"
```

The worker and maintenance loops visit each active company with a scoped session.
Tenant connections use NullPool to avoid keeping idle pools per customer. Size
the PostgreSQL server and API concurrency for expected tenant and click volumes.
The health endpoint checks the selected company's migration revision when routed
to a company, and the control database otherwise.

Free Render's database expiration, shared resource limits and sleeping web service
still apply. Separate logical databases share the server's CPU, storage and
connection limits. This change does not purchase hosting, a domain, or paid plans.

Validation: scripts/test_postgres.py runs real disposable database provisioning
with a non-superuser provisioner, distinct databases and roles, duplicate-name
handling, cross-company token rejection, publisher creation, tracking/postbacks,
CSRF origin checks and worker routing. The generated test databases/roles are
cleaned up after the test.
