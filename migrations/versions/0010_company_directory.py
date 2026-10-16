"""Control-plane company directory; no legacy customer data is moved automatically."""

from alembic import op
import sqlalchemy as sa

revision = "0010_company_directory"
down_revision = "0009_payout_actor"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "company_directory",
        sa.Column("slug", sa.String(63), primary_key=True),
        sa.Column("database_name", sa.String(63), unique=True, nullable=False),
        sa.Column("database_secret", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.String(32), unique=True),
        sa.Column("status", sa.String(20), nullable=False),
    )
    op.create_index("ix_company_directory_status", "company_directory", ["status"])
    op.create_table(
        "company_hostnames",
        sa.Column("hostname", sa.String(253), primary_key=True),
        sa.Column("slug", sa.String(63), nullable=False),
    )
    op.create_index("ix_company_hostnames_slug", "company_hostnames", ["slug"])


def downgrade():
    op.drop_table("company_hostnames")
    op.drop_table("company_directory")
