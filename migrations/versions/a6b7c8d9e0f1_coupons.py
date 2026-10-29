"""coupons"""

from alembic import op
import sqlalchemy as sa


revision = "a6b7c8d9e0f1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "coupons",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("offer_id", sa.String(length=32), nullable=False),
        sa.Column("partner_id", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("quota", sa.Integer(), nullable=True),
        sa.Column("uses", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quota IS NULL OR quota > 0", name="ck_coupon_quota"),
        sa.CheckConstraint("uses >= 0", name="ck_coupon_uses"),
        sa.ForeignKeyConstraint(["workspace_id", "offer_id"], ["offers.workspace_id", "offers.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "partner_id"], ["users.workspace_id", "users.id"]
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.UniqueConstraint("workspace_id", "code"),
    )


def downgrade():
    op.drop_table("coupons")
