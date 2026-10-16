"""Separate local company-mode database; never modifies the existing app database.

Uses only the project-local PostgreSQL server created by dev_postgres.py.
Run `python scripts/company_dev.py prepare` or `python scripts/company_dev.py serve`.
"""

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import json

from cryptography.fernet import Fernet
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from psycopg import sql


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "serve"])
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--frontend-port", type=int, default=3001)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    values = dotenv_values(root / ".env")
    url = make_url(values["DATABASE_URL"])
    if url.host != "127.0.0.1" or url.port != 5433 or url.username != "affiliate_app":
        raise SystemExit("This helper only supports project-local PostgreSQL on 127.0.0.1:5433.")
    database = "affiliate_company_dev"
    admin_url = url.set(
        database="postgres",
        username="postgres",
        password=(root / ".postgres-admin-password").read_text(encoding="ascii"),
    )
    admin = create_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool, hide_parameters=True
    )
    with admin.connect() as connection:
        owner = connection.execute(
            text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=:name"),
            {"name": database},
        ).scalar()
        if owner and owner != url.username:
            raise SystemExit(
                "Existing development database has an unexpected owner; refusing to change it."
            )
        if not owner:
            connection.exec_driver_sql(
                sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0")
                .format(sql.Identifier(database), sql.Identifier(url.username))
                .as_string()
            )
    admin.dispose()
    key_path = root / ".company-dev-key"
    if not key_path.exists():
        key_path.write_bytes(Fernet.generate_key())
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": url.set(database=database).render_as_string(hide_password=False),
            "COMPANY_DATABASE_ADMIN_URL": admin_url.render_as_string(hide_password=False),
            "COMPANY_DATABASES": "true",
            "COMPANY_BASE_DOMAIN": "localhost",
            "COMPANY_FRONTEND_PORT": str(args.frontend_port),
            "ENCRYPTION_KEY": key_path.read_text(),
            "ENVIRONMENT": "development",
            "PUBLIC_BASE_URL": f"http://127.0.0.1:{args.port}",
            "FRONTEND_BASE_URL": f"http://localhost:{args.frontend_port}",
            "CORS_ORIGINS": json.dumps([f"http://localhost:{args.frontend_port}"]),
            "ALLOWED_HOSTS": '["127.0.0.1","localhost"]',
            "REQUIRE_EMAIL_VERIFICATION": "false",
        }
    )

    def run(*arguments):
        subprocess.run([sys.executable, *arguments], cwd=root, env=environment, check=True)

    run("-m", "alembic", "upgrade", "head")
    run("-c", "from app.tenancy import migrate_companies; migrate_companies()")
    print(
        f"Company development mode ready. API: http://127.0.0.1:{args.port}; frontend: http://localhost:{args.frontend_port}",
        flush=True,
    )
    if args.action == "prepare":
        return
    commands = [
        ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.port)],
        ["-m", "app.worker"],
        ["-m", "app.maintenance"],
    ]
    children = []
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for command in commands:
            children.append(subprocess.Popen([sys.executable, *command], cwd=root, env=environment))
        while not stopping:
            if any(child.poll() is not None for child in children):
                raise SystemExit("A company development process stopped; inspect its logs.")
            time.sleep(0.5)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    main()
