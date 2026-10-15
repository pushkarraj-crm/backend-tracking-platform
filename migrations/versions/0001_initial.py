"""Initial multi-tenant affiliate schema.

This revision contains frozen table definitions and does not import application models.
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("workspace_id", sa.String(32), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("workspace_id", "email"),
        sa.UniqueConstraint("workspace_id", "id"),
    )
    op.create_index("ix_users_workspace_id", "users", ["workspace_id"])
    op.create_table(
        "offers",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("workspace_id", sa.String(32), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("destination_url", sa.String(2048), nullable=False),
        sa.Column("revenue", sa.Numeric(14, 4), nullable=False),
        sa.Column("payout", sa.Numeric(14, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("postback_key_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("workspace_id", "id"),
    )
    op.create_index("ix_offers_workspace_id", "offers", ["workspace_id"])
    op.create_table(
        "tracking_links",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("workspace_id", sa.String(32), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("offer_id", sa.String(32), nullable=False),
        sa.Column("affiliate_id", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "affiliate_id"], ["users.workspace_id", "users.id"]
        ),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.UniqueConstraint("workspace_id", "offer_id", "affiliate_id"),
    )
    op.create_index("ix_tracking_links_workspace_id", "tracking_links", ["workspace_id"])
    op.create_table(
        "clicks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("workspace_id", sa.String(32), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("link_id", sa.String(32), nullable=False),
        sa.Column("sub_id", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "link_id"], ["tracking_links.workspace_id", "tracking_links.id"]
        ),
        sa.UniqueConstraint("workspace_id", "id"),
    )
    op.create_index("ix_clicks_link_id", "clicks", ["link_id"])
    op.create_index("ix_click_workspace_created", "clicks", ["workspace_id", "created_at"])
    op.create_table(
        "conversions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("workspace_id", sa.String(32), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("click_id", sa.String(32), nullable=False),
        sa.Column("offer_id", sa.String(32), nullable=False),
        sa.Column("transaction_id", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revenue", sa.Numeric(14, 4), nullable=False),
        sa.Column("payout", sa.Numeric(14, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id", "click_id"], ["clicks.workspace_id", "clicks.id"]),
        sa.ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        sa.UniqueConstraint("workspace_id", "offer_id", "transaction_id"),
        sa.UniqueConstraint("workspace_id", "click_id"),
    )
    op.create_index(
        "ix_conversion_workspace_created", "conversions", ["workspace_id", "created_at"]
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("workspace_id", sa.String(32), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("actor_id", sa.String(32), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("resource_id", sa.String(32), nullable=False),
        sa.Column("detail", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_events_workspace_id", "audit_events", ["workspace_id"])


def downgrade():
    for table in (
        "audit_events",
        "conversions",
        "clicks",
        "tracking_links",
        "offers",
        "users",
        "workspaces",
    ):
        op.drop_table(table)
