from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
sqlite_test = settings.database_url.startswith("sqlite")
engine_options = (
    {"connect_args": {"check_same_thread": False, "timeout": 15}}
    if sqlite_test
    else {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        "pool_recycle": 1800,
        "connect_args": {
            "connect_timeout": 5,
            "application_name": "affiliate-api",
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms} -c timezone=UTC"
            + (f" -c search_path={settings.db_schema}" if settings.db_schema else ""),
        },
    }
)
engine = create_engine(
    settings.database_url, pool_pre_ping=True, hide_parameters=True, **engine_options
)
if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")


SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db():
    with SessionLocal() as session:
        yield session
