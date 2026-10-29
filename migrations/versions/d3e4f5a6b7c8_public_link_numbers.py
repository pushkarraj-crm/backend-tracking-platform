"""public_link_numbers"""

from alembic import op
import sqlalchemy as sa


revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade():
    # Numbers are assigned when a link is first shown; no backfill needed.
    for table in ("offers", "users"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("public_number", sa.Integer(), nullable=True))
            batch.create_unique_constraint(f"{table}_public_number_key", ["public_number"])


def downgrade():
    for table in ("offers", "users"):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"{table}_public_number_key", type_="unique")
            batch.drop_column("public_number")
