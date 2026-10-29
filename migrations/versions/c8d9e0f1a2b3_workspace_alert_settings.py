"""workspace_alert_settings"""

from alembic import op
import sqlalchemy as sa


revision = "c8d9e0f1a2b3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade():
    # Encrypted Slack / Telegram alert configuration; empty until an admin sets it up.
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("alert_settings", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("alert_settings")
