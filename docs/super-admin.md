# Super admin

The standalone app is in `D:\Tracking platform\Super Admin`, repository
[super-admin-frontend-tracking-platform](https://github.com/pushkarraj-crm/super-admin-frontend-tracking-platform).
The normal network frontend remains separate. All management endpoints require
the authenticated server-side `platform_admin` role; no public role selector,
registration request or API key can grant it.

## Operator activation

An authorized operator runs this command against the intended backend database:

```
python scripts/grant_owner.py --email pushkar@vedhaka.com
```

The account must already exist, be active and have verified email. Its password
is retained, sessions/API keys are revoked, and an audit entry is recorded. No
default password, startup auto-promotion or public bootstrap endpoint exists.
With company databases, the operator can specify `--company slug` to select an
existing verified identity for control-plane access. Sign in on the super admin
app's control origin, not an individual network subdomain.

## Management

Select a customer network, then open Users and Change access. The `admin` role
means that network's admin dashboard. Other assignable roles are advertiser,
publisher, affiliate and staff. Account enablement, API access, publisher parent
and restrictive staff module permissions are editable. Staff permissions are
`null` for original staff access, `[]` for no management modules, or a supported
permission array. They never upgrade a role to network/platform administrator.

Every access change requires a reason, revokes sessions and API keys and records
the actor/target/role transition in the selected network's audit trail. Users must
sign in again. The last active network admin is protected; assign another admin
first. Affiliate publishers must be reassigned before removing their parent.
Active advertiser campaigns must be paused/reassigned before incompatible role
changes. Existing financial history is preserved. Super admin identities cannot
be edited through tenant account controls.

Endpoints:

- `GET /platform/networks/permissions`
- `GET /platform/networks/{network_id}/users`
- `PATCH /platform/networks/{network_id}/users/{user_id}/access`
- `GET /platform/networks/{network_id}/{overview|campaigns|conversions|invoices|domains|audit}`
- Existing `/platform/summary`, `/platform/workspaces` and network suspension APIs.

Company databases are selected only through the trusted control-plane directory;
requests cannot supply database URLs. Tenant resources remain in their own database.
Views intentionally omit passwords, postback keys, provider credentials and raw
connection details. Financial operations continue through their validated workflows;
this console does not provide an unrestricted financial-row editor or initiate payouts.
