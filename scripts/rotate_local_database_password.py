"""Rotate only the isolated development application's database credential."""

from pathlib import Path
import secrets
from dotenv import dotenv_values, set_key
from sqlalchemy.engine import make_url
import psycopg
from psycopg import sql

root = Path(__file__).resolve().parents[1]
url = make_url(dotenv_values(root / ".env")["DATABASE_URL"])
if (
    url.host != "127.0.0.1"
    or url.port != 5433
    or url.username != "affiliate_app"
    or url.database != "affiliate_platform"
):
    raise SystemExit("This helper is restricted to the project-local development database")
password = secrets.token_urlsafe(48)
with psycopg.connect(
    host="127.0.0.1",
    port=5433,
    user="postgres",
    dbname="postgres",
    password=(root / ".postgres-admin-password").read_text(encoding="ascii"),
    autocommit=True,
) as connection:
    connection.execute(
        sql.SQL("ALTER ROLE affiliate_app PASSWORD {}").format(sql.Literal(password))
    )
set_key(
    str(root / ".env"),
    "DATABASE_URL",
    url.set(password=password).render_as_string(hide_password=False),
)
print("Project-local application database credential rotated.")
