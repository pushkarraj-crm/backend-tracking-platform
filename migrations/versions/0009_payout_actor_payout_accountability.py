"""Payout actor accountability and tenant-bound reversal relationships."""

from alembic import op
import sqlalchemy as sa

revision = "0009_payout_actor"
down_revision = "0008_scheduler"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("payout_providers") as batch:
        batch.add_column(sa.Column("configured_by", sa.String(32), nullable=True))
        batch.add_column(
            sa.Column("beneficiary_cursor", sa.String(32), server_default="", nullable=False)
        )
        batch.create_foreign_key("fk_provider_actor", "users", ["configured_by"], ["id"])
    with op.batch_alter_table("payout_requests") as batch:
        batch.add_column(sa.Column("requested_by", sa.String(32), nullable=True))
        batch.create_unique_constraint("uq_payout_request_tenant_id", ["workspace_id", "id"])
        batch.create_foreign_key("fk_payout_request_actor", "users", ["requested_by"], ["id"])
    with op.batch_alter_table("payout_adjustments") as batch:
        batch.create_foreign_key(
            "fk_adjustment_request",
            "payout_requests",
            ["workspace_id", "request_id"],
            ["workspace_id", "id"],
        )
        batch.create_foreign_key(
            "fk_adjustment_recipient",
            "users",
            ["workspace_id", "affiliate_id"],
            ["workspace_id", "id"],
        )


def downgrade():
    with op.batch_alter_table("payout_adjustments") as batch:
        batch.drop_constraint("fk_adjustment_request", type_="foreignkey")
        batch.drop_constraint("fk_adjustment_recipient", type_="foreignkey")
    with op.batch_alter_table("payout_requests") as batch:
        batch.drop_constraint("fk_payout_request_actor", type_="foreignkey")
        batch.drop_constraint("uq_payout_request_tenant_id", type_="unique")
        batch.drop_column("requested_by")
    with op.batch_alter_table("payout_providers") as batch:
        batch.drop_constraint("fk_provider_actor", type_="foreignkey")
        batch.drop_column("configured_by")
        batch.drop_column("beneficiary_cursor")
