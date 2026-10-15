"""Email/password login with globally unique identities; remove MFA."""

from alembic import op
import sqlalchemy as sa

revision = "0005_email_login"
down_revision = "0004_registration"
branch_labels = None
depends_on = None


def upgrade():
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM (SELECT lower(email) FROM users GROUP BY lower(email) HAVING count(*) > 1) duplicates"
            )
        )
        .scalar()
    )
    if duplicates:
        raise RuntimeError(
            "Existing accounts have duplicate emails; resolve identity ownership before migrating. No accounts were merged or deleted."
        )
    op.execute("UPDATE users SET auth_version=auth_version+1 WHERE mfa_enabled=true")
    op.execute(
        "UPDATE auth_sessions SET revoked=true WHERE user_id IN (SELECT id FROM users WHERE mfa_enabled=true)"
    )
    op.execute("UPDATE users SET email=lower(email)")
    with op.batch_alter_table("users") as batch:
        batch.create_unique_constraint("uq_user_email_global", ["email"])
        batch.drop_column("mfa_secret")
        batch.drop_column("mfa_last_step")
        batch.drop_column("mfa_enabled")


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("mfa_enabled", sa.Boolean(), server_default="false", nullable=False)
        )
        batch.add_column(
            sa.Column("mfa_last_step", sa.Integer(), server_default="-1", nullable=False)
        )
        batch.add_column(sa.Column("mfa_secret", sa.Text(), nullable=True))
        batch.drop_constraint("uq_user_email_global", type_="unique")
