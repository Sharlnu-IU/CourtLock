"""GET /courts/{id}/availability (public, no identity needed)."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from courtlock.api.deps import DbSession
from courtlock.api.schemas import AvailabilityResponse, CourtsResponse
from courtlock.services import availability, catalog

router = APIRouter(tags=["courts"])


@router.get("/courts/{court_id}/availability", response_model=AvailabilityResponse)
def get_court_availability(
    court_id: uuid.UUID,
    session: DbSession,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
) -> AvailabilityResponse:
    """Slots for venue-local dates [from, to). Advisory only: a hold is what reserves a slot."""
    result = availability.get_availability(session, court_id, from_date, to_date)
    return AvailabilityResponse.from_availability(result)


@router.get("/courts", response_model=CourtsResponse)
def get_courts(session: DbSession) -> CourtsResponse:
    """Active courts with their venue, so a client can discover court ids."""
    return CourtsResponse.from_listing(catalog.list_courts(session))
