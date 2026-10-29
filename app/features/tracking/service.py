import secrets

from sqlalchemy import select

from app.persistence.models import Offer, User


def public_number(db, row):
    """Short public ID for a campaign or partner, unique across accounts; assigned on first use."""
    if row.public_number is None:
        model = type(row)
        # shortcut: random 6-digit IDs (900k per kind); widen the range before that fills up.
        while True:
            number = 100000 + secrets.randbelow(900000)
            if not db.scalar(select(model.id).where(model.public_number == number)):
                break
        row.public_number = number
        db.flush()
    return row.public_number


def link_url(db, base, link):
    """Offer18-style tracking link: ?o=campaign&a=partner."""
    offer, partner = db.get(Offer, link.offer_id), db.get(User, link.affiliate_id)
    return f"{base}/c?o={public_number(db, offer)}&a={public_number(db, partner)}"
