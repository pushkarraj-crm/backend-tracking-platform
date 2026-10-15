"""Backfill cap counters from durable historical clicks and conversions."""

from decimal import Decimal
import uuid
from alembic import op
import sqlalchemy as sa

revision = "0007_counter_backfill"
down_revision = "0006_advanced"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    postgres = connection.dialect.name == "postgresql"
    if postgres:
        # Run this first-release backfill in maintenance mode. Block counter writes first.
        connection.execute(sa.text("LOCK TABLE offer_counters, clicks, conversions IN SHARE MODE"))
    date_expression = (
        "to_char(created_at AT TIME ZONE 'UTC','YYYY-MM-DD')"
        if postgres
        else "substr(created_at,1,10)"
    )
    table = sa.table(
        "offer_counters",
        sa.column("id", sa.String),
        sa.column("workspace_id", sa.String),
        sa.column("offer_id", sa.String),
        sa.column("period", sa.String),
        sa.column("clicks", sa.Integer),
        sa.column("conversions", sa.Integer),
        sa.column("revenue", sa.Numeric),
    )
    cursor = ""
    while True:
        offers = (
            connection.execute(
                sa.text(
                    "SELECT id,workspace_id FROM offers WHERE id>:cursor ORDER BY id LIMIT 500"
                ),
                {"cursor": cursor},
            )
            .mappings()
            .all()
        )
        if not offers:
            break
        for offer in offers:
            counts = {}
            parameters = {"offer": offer["id"], "workspace": offer["workspace_id"]}
            click_dates = date_expression.replace("created_at", "c.created_at")
            for row in connection.execute(
                sa.text(
                    f"SELECT {click_dates} AS period,count(*) AS clicks FROM clicks c JOIN tracking_links l ON c.link_id=l.id WHERE l.offer_id=:offer AND c.workspace_id=:workspace GROUP BY {click_dates}"
                ),
                parameters,
            ).mappings():
                counts[row["period"]] = {
                    "clicks": row["clicks"],
                    "conversions": 0,
                    "revenue": Decimal(0),
                }
            for row in connection.execute(
                sa.text(
                    f"SELECT {date_expression} AS period,count(*) AS conversions,sum(revenue) AS revenue FROM conversions WHERE offer_id=:offer AND workspace_id=:workspace GROUP BY {date_expression}"
                ),
                parameters,
            ).mappings():
                count = counts.setdefault(
                    row["period"], {"clicks": 0, "conversions": 0, "revenue": Decimal(0)}
                )
                count.update(conversions=row["conversions"], revenue=Decimal(str(row["revenue"])))
            counts["total"] = {
                "clicks": sum(c["clicks"] for c in counts.values()),
                "conversions": sum(c["conversions"] for c in counts.values()),
                "revenue": sum((c["revenue"] for c in counts.values()), Decimal(0)),
            }
            for period, count in counts.items():
                existing = connection.execute(
                    sa.text(
                        "SELECT id FROM offer_counters WHERE workspace_id=:workspace AND offer_id=:offer AND period=:period"
                    ),
                    {**parameters, "period": period},
                ).scalar()
                if existing:
                    connection.execute(
                        sa.text(
                            "UPDATE offer_counters SET clicks=:clicks,conversions=:conversions,revenue=:revenue WHERE id=:id"
                        ),
                        {**count, "revenue": str(count["revenue"]), "id": existing},
                    )
                else:
                    op.bulk_insert(
                        table,
                        [
                            {
                                "id": uuid.uuid4().hex,
                                "workspace_id": offer["workspace_id"],
                                "offer_id": offer["id"],
                                "period": period,
                                **count,
                            }
                        ],
                    )
        cursor = offers[-1]["id"]


def downgrade():
    # Counters are valid data and must survive a code-only rollback.
    pass
