from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import get_settings
from app.core.database import Base
from app.persistence import models as models  # noqa: F401 -- register every mapped table before schema comparison
from app.persistence import network_models as network_models  # noqa: F401 -- register network operations tables
from app.persistence import tenancy as tenancy  # noqa: F401 -- register control-plane directory tables

config = context.config
target_metadata = Base.metadata
database_url = get_settings().database_url


def run_online(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    provided_connection = config.attributes.get("connection")
    if provided_connection is not None:
        run_online(provided_connection)
    else:
        engine = create_engine(database_url, poolclass=pool.NullPool)
        with engine.connect() as connection:
            run_online(connection)
        engine.dispose()
