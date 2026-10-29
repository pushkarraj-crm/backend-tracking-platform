"""team_logins_and_account_managers"""

from alembic import op
import sqlalchemy as sa


revision = "d9e0f1a2b3c4"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "team_members",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("full_name", sa.String(length=120), nullable=True),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id", "owner_id"], ["users.workspace_id", "users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_team_members_owner_id", "team_members", ["owner_id"])
    with op.batch_alter_table("auth_sessions") as batch:
        # Who is really signed in when it is not the user: an admin using "log in as",
        # or a member of the user's team.
        batch.add_column(sa.Column("acted_by", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("team_member_id", sa.String(length=32), nullable=True))
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("manager_id", sa.String(length=32), nullable=True))
        batch.create_index("ix_users_manager_id", ["manager_id"])


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_manager_id")
        batch.drop_column("manager_id")
    with op.batch_alter_table("auth_sessions") as batch:
        batch.drop_column("team_member_id")
        batch.drop_column("acted_by")
    op.drop_index("ix_team_members_owner_id", table_name="team_members")
    op.drop_table("team_members")
