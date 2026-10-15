"""Manage an isolated local PostgreSQL cluster; never touches existing installations' data."""

import argparse
from pathlib import Path
import secrets
import subprocess
import sys
import psycopg
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from psycopg import sql
from dotenv import set_key

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".postgres"
ADMIN_PASSWORD = ROOT / ".postgres-admin-password"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "start", "stop", "status"])
    parser.add_argument("--bin", default=r"C:\Program Files\PostgreSQL\16\bin")
    args = parser.parse_args()
    binaries = Path(args.bin)
    pg_ctl = str(binaries / "pg_ctl.exe")
    if not Path(pg_ctl).is_file():
        raise SystemExit("PostgreSQL binaries missing; supply --bin with their directory.")

    def run(*command):
        subprocess.run(command, cwd=ROOT, check=True)

    def start():
        status = subprocess.run([pg_ctl, "-D", str(DATA), "status"], capture_output=True)
        if status.returncode != 0:
            run(pg_ctl, "-D", str(DATA), "-l", str(DATA / "server.log"), "-w", "start")

    if args.command == "init":
        if (DATA / "PG_VERSION").exists():
            raise SystemExit(
                "Project cluster already initialized; use start, then alembic upgrade head."
            )
        if DATA.exists():
            raise SystemExit("Data directory already exists; inspect it before initialization.")
        admin_password = secrets.token_urlsafe(48)
        ADMIN_PASSWORD.write_text(admin_password, encoding="ascii")
        run(
            str(binaries / "initdb.exe"),
            "-D",
            str(DATA),
            "-U",
            "postgres",
            "--pwfile",
            str(ADMIN_PASSWORD),
            "--auth-host=scram-sha-256",
            "--auth-local=scram-sha-256",
            "--encoding=UTF8",
            "--locale=C",
        )
        with (DATA / "postgresql.conf").open("a", encoding="ascii") as configuration:
            configuration.write(
                "\nlisten_addresses = '127.0.0.1'\nport = 5433\npassword_encryption = 'scram-sha-256'\n"
            )
        start()
        app_password = secrets.token_urlsafe(48)
        with psycopg.connect(
            host="127.0.0.1",
            port=5433,
            user="postgres",
            password=admin_password,
            dbname="postgres",
            autocommit=True,
        ) as connection:
            connection.execute(
                sql.SQL(
                    "CREATE ROLE affiliate_app LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE"
                ).format(sql.Literal(app_password))
            )
            connection.execute("CREATE DATABASE affiliate_platform OWNER affiliate_app")
            connection.execute("CREATE DATABASE affiliate_test OWNER affiliate_app")
        env_path = ROOT / ".env"
        if not env_path.exists():
            env_path.write_text("JWT_SECRET=" + secrets.token_urlsafe(48) + "\n", encoding="ascii")
        url = f"postgresql+psycopg://affiliate_app:{app_password}@127.0.0.1:5433/affiliate_platform"
        set_key(str(env_path), "DATABASE_URL", url)
        set_key(str(env_path), "AUTO_CREATE_TABLES", "false")
        values = dotenv_values(env_path)
        if (
            not values.get("ENCRYPTION_KEY")
            or values.get("ENCRYPTION_KEY") == "REPLACE_WITH_FERNET_KEY"
        ):
            set_key(str(env_path), "ENCRYPTION_KEY", Fernet.generate_key().decode())
        if not values.get("METRICS_TOKEN"):
            set_key(str(env_path), "METRICS_TOKEN", secrets.token_urlsafe(48))
        print(
            "Project PostgreSQL initialized on 127.0.0.1:5433. Credentials saved to ignored .env."
        )
        run(sys.executable, "-m", "alembic", "upgrade", "head")
    elif args.command == "start":
        start()
    elif args.command == "stop":
        run(pg_ctl, "-D", str(DATA), "-w", "-m", "fast", "stop")
    else:
        run(pg_ctl, "-D", str(DATA), "status")


if __name__ == "__main__":
    main()
