"""global_postback_token"""

from alembic import op
import sqlalchemy as sa


revision = "b1c2d3e4f5a6"
down_revision = "70c824fcf3bd"
branch_labels = None
depends_on = None


def upgrade():
    # Tokens are created on first use; existing workspaces need no backfill.
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("postback_token_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("postback_token", sa.Text(), nullable=True))
        batch.create_unique_constraint(
            "workspaces_postback_token_hash_key", ["postback_token_hash"]
        )


def downgrade():
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_constraint("workspaces_postback_token_hash_key", type_="unique")
        batch.drop_column("postback_token")
        batch.drop_column("postback_token_hash")
