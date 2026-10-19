"""Render personal preview: migrate, then supervise API, worker and maintenance together.

This deliberately follows the free web service's sleep/restart lifecycle. It does
not keep the service awake or promise continuously available background delivery.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit


def prepare_environment(environ):
    database = environ.get("DATABASE_URL", "")
    if database.startswith("postgres://"):
        environ["DATABASE_URL"] = "postgresql+psycopg://" + database[len("postgres://") :]
    elif database.startswith("postgresql://"):
        environ["DATABASE_URL"] = "postgresql+psycopg://" + database[len("postgresql://") :]
    public = environ.get("PUBLIC_BASE_URL") or environ.get("RENDER_EXTERNAL_URL", "")
    frontend = environ.get("FRONTEND_BASE_URL", "")
    if not public.startswith("https://") or not frontend.startswith("https://"):
        raise ValueError("Set HTTPS PUBLIC_BASE_URL/RENDER_EXTERNAL_URL and FRONTEND_BASE_URL")
    environ["PUBLIC_BASE_URL"] = public.rstrip("/")
    environ.setdefault("ALLOWED_HOSTS", json.dumps([urlsplit(public).hostname]))
    environ.setdefault("CORS_ORIGINS", json.dumps([frontend.rstrip("/")]))


def main():
    os.chdir(Path(__file__).resolve().parents[1])
    prepare_environment(os.environ)
    # The managed database is fresh for this preview. Existing data is preserved.
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    subprocess.run(
        [sys.executable, "-c", "from app.tenancy import migrate_companies; migrate_companies()"],
        check=True,
    )
    # Operator-only opt-in for free hosts without an interactive shell. No default owner.
    owner_email = os.environ.get("INITIAL_PLATFORM_OWNER_EMAIL")
    if owner_email:
        subprocess.run(
            [sys.executable, "scripts/grant_owner.py", "--email", owner_email, "--initial-only"],
            check=True,
        )
    port = str(int(os.environ.get("PORT", "10000")))
    commands = [
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            port,
            "--proxy-headers",
            "--forwarded-allow-ips",
            "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
            "--no-access-log",
            "--limit-concurrency",
            "50",
        ],
        [sys.executable, "-m", "app.worker"],
        [sys.executable, "-m", "app.maintenance"],
    ]
    children = []
    stopping = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for command in commands:
            children.append(subprocess.Popen(command))
        while not stopping:
            if any(child.poll() is not None for child in children):
                return 1
            time.sleep(0.5)
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 20
        for child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
