"""Independent scheduler; keep slow DNS/provider maintenance away from delivery workers."""

import logging
import signal
import time
from datetime import timedelta

from sqlalchemy import delete, select

from app.core.database import SessionLocal
from app.core.services import enqueue
from app.features.domains.router import revalidate_domains
from app.persistence.models import FraudCounter, OutboxEvent, Workspace, now

stopping = False
logger = logging.getLogger("affiliate.maintenance")


def run_once():
    with SessionLocal.begin() as db:
        subscriptions = list(
            db.scalars(
                select(Workspace)
                .where(
                    Workspace.razorpay_subscription_id.is_not(None),
                    Workspace.next_billing_check_at <= now(),
                )
                .order_by(Workspace.next_billing_check_at, Workspace.id)
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        )
        for workspace in subscriptions:
            pending = db.scalar(
                select(OutboxEvent.id)
                .where(
                    OutboxEvent.workspace_id == workspace.id,
                    OutboxEvent.kind == "billing",
                    OutboxEvent.status.in_(["pending", "processing"]),
                )
                .limit(1)
            )
            if not pending:
                enqueue(
                    db,
                    workspace.id,
                    "billing",
                    {"subscription_id": workspace.razorpay_subscription_id},
                )
            workspace.next_billing_check_at = now() + timedelta(minutes=15)
        from app.features.tools.automation import run_automations

        run_automations(db)
        # Expired velocity buckets are operational state, not financial history.
        db.execute(delete(FraudCounter).where(FraudCounter.minute < int(time.time()) // 60 - 1440))
    revalidate_domains()


def stop(signum, frame):
    global stopping
    stopping = True


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        try:
            from app.tenancy.service import for_each_company

            for_each_company(run_once)
        except Exception as error:
            logger.error("maintenance failed: %s", type(error).__name__)
        for _ in range(60):
            if stopping:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
