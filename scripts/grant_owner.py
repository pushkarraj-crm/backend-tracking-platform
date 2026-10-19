"""Operator-only promotion of an existing account; never creates a default password."""

import argparse
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import TypeAdapter, EmailStr  # noqa: E402
from sqlalchemy import select, update  # noqa: E402
from app.db import ControlSession  # noqa: E402
from app.models import Workspace, User, AuthSession, AuditEvent  # noqa: E402
from app.network_models import PartnerApiKey  # noqa: E402
from app.config import get_settings  # noqa: E402


def grant_owner(email, company_slug=None, initial_only=False):
    email = str(TypeAdapter(EmailStr).validate_python(email)).lower()
    with ControlSession.begin() as db:
        if initial_only:
            existing = db.scalar(
                select(User).where(User.role == "platform_admin").with_for_update()
            )
            if existing:
                if existing.email != email or not existing.active:
                    raise ValueError(
                        "A different or disabled owner already exists; use the manual operator procedure"
                    )
                return existing.id
        account = db.scalar(select(User).where(User.email == email).with_for_update())
        if not account and get_settings().company_databases:
            from app.tenancy import active_companies, company_sessions

            matches = []
            for company in active_companies():
                if company_slug and company.slug != company_slug:
                    continue
                with company_sessions(company)() as tenant:
                    member = tenant.scalar(
                        select(User).where(
                            User.email == email,
                            User.active.is_(True),
                            User.email_verified.is_(True),
                        )
                    )
                    if member:
                        matches.append(member.password_hash)
            if len(matches) != 1:
                raise ValueError(
                    "Select one existing verified account with --company; no account was changed"
                )
            workspace = Workspace(
                name="Platform operations",
                plan="business",
                subscription_status="active",
                paid_until=datetime.now(timezone.utc) + timedelta(days=36500),
            )
            db.add(workspace)
            db.flush()
            account = User(
                workspace_id=workspace.id,
                email=email,
                password_hash=matches[0],
                role="platform_admin",
                email_verified=True,
            )
            db.add(account)
            db.flush()
        if not account or not account.active or not account.email_verified:
            raise ValueError(
                "An existing active, verified account is required; no account was changed"
            )
        workspace = db.get(Workspace, account.workspace_id)
        if not workspace or not workspace.active:
            raise ValueError("The account's workspace must be active")
        before = account.role
        account.role = "platform_admin"
        account.parent_user_id = None
        account.staff_permissions = None
        account.api_enabled = False
        account.auth_version += 1
        db.execute(
            update(AuthSession).where(AuthSession.user_id == account.id).values(revoked=True)
        )
        db.execute(
            update(PartnerApiKey).where(PartnerApiKey.user_id == account.id).values(active=False)
        )
        db.add(
            AuditEvent(
                workspace_id=account.workspace_id,
                actor_id=account.id,
                action="platform.owner_granted",
                resource_id=account.id,
                detail=f"Operator authorized {before}->platform_admin",
            )
        )
        return account.id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--company", help="Required if the email belongs to multiple companies")
    parser.add_argument(
        "--initial-only",
        action="store_true",
        help="Initialize once; never revoke an existing owner's sessions on restart",
    )
    args = parser.parse_args()
    try:
        grant_owner(args.email, args.company, args.initial_only)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print(
        "Super admin access granted. Sign in at the control-domain super admin app using the existing password."
    )


if __name__ == "__main__":
    main()
