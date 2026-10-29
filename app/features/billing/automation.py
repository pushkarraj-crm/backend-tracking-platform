"""Retired fund-transfer API. Financial history is retained for reconciliation."""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/automated-payouts", tags=["Retired payouts"])


@router.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False
)
def retired_funding(path: str):
    raise HTTPException(
        410,
        "Automated funding has been retired. Advertisers pay publishers externally through invoices.",
    )


def provider_call(*args, **kwargs):
    raise RuntimeError("Platform-operated payouts are disabled")


def queue_request(*args, **kwargs):
    raise HTTPException(410, "Platform-operated payouts are disabled")


def process(db, request_id, workspace_id):
    # Old queued events must never initiate or reconcile fund transfers.
    raise RuntimeError("Legacy payout job disabled; reconcile externally")


def schedule(db):
    return 0
