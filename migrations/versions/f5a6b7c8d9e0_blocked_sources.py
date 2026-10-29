"""blocked_sources"""

from alembic import op
import sqlalchemy as sa


revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "blocked_sources",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("offer_id", sa.String(length=32), nullable=True),
        sa.Column("preset", sa.String(length=30), nullable=False),
        sa.Column("value", sa.String(length=253), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
    )
    op.create_index("ix_blocked_source_workspace", "blocked_sources", ["workspace_id", "active"])
    op.add_column("fraud_events", sa.Column("publisher_id", sa.String(length=32), nullable=True))
    op.create_index(op.f("ix_fraud_events_publisher_id"), "fraud_events", ["publisher_id"])


def downgrade():
    op.drop_index(op.f("ix_fraud_events_publisher_id"), table_name="fraud_events")
    op.drop_column("fraud_events", "publisher_id")
    op.drop_index("ix_blocked_source_workspace", table_name="blocked_sources")
    op.drop_table("blocked_sources")
