"""workspace_web_key"""

from alembic import op
import sqlalchemy as sa


revision = "b7c8d9e0f1a2"
down_revision = "a6b7c8d9e0f1"
branch_labels = None
depends_on = None


def upgrade():
    # Keys are created when a store integration is first set up; no backfill needed.
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("web_key", sa.String(length=32), nullable=True))
        batch.create_unique_constraint("workspaces_web_key_key", ["web_key"])


def downgrade():
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_constraint("workspaces_web_key_key", type_="unique")
        batch.drop_column("web_key")
