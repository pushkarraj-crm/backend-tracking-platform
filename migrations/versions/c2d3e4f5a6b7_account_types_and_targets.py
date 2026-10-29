"""account_types_and_targets"""

from alembic import op
import sqlalchemy as sa


revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workspaces",
        sa.Column("account_type", sa.String(length=20), server_default="network", nullable=False),
    )
    # Brands that signed up before account types existed become brand accounts.
    op.execute(
        "UPDATE workspaces SET account_type = 'brand' "
        "WHERE business_type = 'brand_direct_advertiser'"
    )
    op.create_table(
        "campaign_targets",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("offer_id", sa.String(length=32), nullable=False),
        sa.Column("partner_id", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_campaign_target_amount"),
        sa.CheckConstraint("ends_on >= starts_on", name="ck_campaign_target_dates"),
        sa.ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        sa.ForeignKeyConstraint(["workspace_id", "partner_id"], ["users.workspace_id", "users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_campaign_target_campaign", "campaign_targets", ["workspace_id", "offer_id", "id"]
    )


def downgrade():
    op.drop_index("ix_campaign_target_campaign", table_name="campaign_targets")
    op.drop_table("campaign_targets")
    op.drop_column("workspaces", "account_type")
