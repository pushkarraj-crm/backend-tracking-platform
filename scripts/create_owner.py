"""Create the platform owner interactively; passwords never appear in command arguments."""

import getpass
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import EmailStr, TypeAdapter
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.security import passwords
from app.persistence.models import User, Workspace


def main():
    email = str(TypeAdapter(EmailStr).validate_python(input("Platform owner email: "))).lower()
    password = getpass.getpass("Owner password (at least 16 characters): ")
    if len(password) < 16 or len(password) > 128:
        raise SystemExit("Password must be 16–128 characters.")
    if getpass.getpass("Confirm password: ") != password:
        raise SystemExit("Passwords do not match.")
    with SessionLocal.begin() as db:
        if db.scalar(select(User.id).where(User.role == "platform_admin")):
            raise SystemExit("An owner already exists. Use account recovery instead.")
        workspace = Workspace(
            name="Platform operations",
            subscription_status="active",
            plan="business",
            paid_until=datetime.now(timezone.utc) + timedelta(days=36500),
        )
        db.add(workspace)
        db.flush()
        db.add(
            User(
                workspace_id=workspace.id,
                email=email,
                password_hash=passwords.hash(password),
                role="platform_admin",
                email_verified=True,
            )
        )
    print("Owner account created. Sign in using its email and password.")


if __name__ == "__main__":
    main()
