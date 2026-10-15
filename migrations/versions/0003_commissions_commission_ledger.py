"""commission_ledger"""

from alembic import op
import sqlalchemy as sa


revision = "0003_commissions"
down_revision = "0002_production"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("conversions") as batch:
        batch.add_column(sa.Column("version", sa.Integer(), server_default="0", nullable=False))
        batch.create_unique_constraint("uq_conversion_workspace_id", ["workspace_id", "id"])
    op.create_table(
        "payouts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("affiliate_id", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("reference", sa.String(length=120), nullable=False),
        sa.Column("recorded_by", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_payout_positive"),
        sa.ForeignKeyConstraint(
            ["recorded_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "affiliate_id"],
            ["users.workspace_id", "users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "reference"),
    )
    op.create_index(
        "ix_payout_account", "payouts", ["workspace_id", "affiliate_id", "currency"], unique=False
    )
    op.create_table(
        "commission_entries",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("affiliate_id", sa.String(length=32), nullable=False),
        sa.Column("conversion_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "affiliate_id"],
            ["users.workspace_id", "users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversion_id"],
            ["conversions.workspace_id", "conversions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "conversion_id", "version"),
    )
    op.create_index(
        "ix_commission_account",
        "commission_entries",
        ["workspace_id", "affiliate_id", "currency"],
        unique=False,
    )
    # Preserve balances for approved conversions created before the ledger existed.
    import uuid

    ledger = sa.table(
        "commission_entries",
        sa.column("id", sa.String),
        sa.column("workspace_id", sa.String),
        sa.column("affiliate_id", sa.String),
        sa.column("conversion_id", sa.String),
        sa.column("version", sa.Integer),
        sa.column("amount", sa.Numeric),
        sa.column("currency", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    cursor = ""
    while True:
        rows = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT c.id,c.workspace_id,c.payout,c.currency,c.created_at,l.affiliate_id "
                    "FROM conversions c JOIN clicks k ON k.id=c.click_id JOIN tracking_links l ON l.id=k.link_id "
                    "WHERE c.status='approved' AND c.id > :cursor ORDER BY c.id LIMIT 1000"
                ),
                {"cursor": cursor},
            )
            .mappings()
            .all()
        )
        if not rows:
            break
        op.bulk_insert(
            ledger,
            [
                {
                    "id": uuid.uuid5(uuid.NAMESPACE_URL, "commission:" + r["id"]).hex,
                    "workspace_id": r["workspace_id"],
                    "affiliate_id": r["affiliate_id"],
                    "conversion_id": r["id"],
                    "version": 0,
                    "amount": r["payout"],
                    "currency": r["currency"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        )
        cursor = rows[-1]["id"]


def downgrade():
    op.drop_table("commission_entries")
    op.drop_table("payouts")
    with op.batch_alter_table("conversions") as batch:
        batch.drop_constraint("uq_conversion_workspace_id", type_="unique")
        batch.drop_column("version")
