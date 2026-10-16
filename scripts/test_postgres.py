"""Run API tests against the dedicated PostgreSQL test database, without exposing credentials."""

import os
from pathlib import Path
import subprocess
import sys
from dotenv import dotenv_values
from sqlalchemy.engine import make_url


def main():
    root = Path(__file__).resolve().parents[1]
    url = make_url(dotenv_values(root / ".env")["DATABASE_URL"])
    if url.get_backend_name() != "postgresql":
        raise SystemExit("Configure PostgreSQL in .env first.")
    environment = os.environ.copy()
    environment["TEST_DATABASE_URL"] = url.set(database="affiliate_test").render_as_string(
        hide_password=False
    )
    password_file = root / ".postgres-admin-password"
    if url.host == "127.0.0.1" and url.port == 5433 and password_file.is_file():
        environment["COMPANY_DATABASE_ADMIN_URL"] = url.set(
            username="postgres",
            database="postgres",
            password=password_file.read_text(encoding="ascii"),
        ).render_as_string(hide_password=False)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *sys.argv[1:]], cwd=root, env=environment
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
