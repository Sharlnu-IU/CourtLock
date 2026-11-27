"""GET /courts: active courts, so a client can discover court ids."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from courtlock.db.models import Court, Venue


@dataclass(frozen=True)
class CourtListing:
    id: uuid.UUID
    name: str
    sport: str
    venue_id: uuid.UUID
    venue_name: str
    timezone: str


def list_courts(session: Session) -> list[CourtListing]:
    with session.begin():
        rows = session.execute(
            select(Court, Venue)
            .join(Venue, Court.venue_id == Venue.id)
            .where(Court.is_active.is_(True))
            .order_by(Venue.name, Court.name)
        ).all()
        return [CourtListing(c.id, c.name, c.sport, v.id, v.name, v.timezone) for c, v in rows]
