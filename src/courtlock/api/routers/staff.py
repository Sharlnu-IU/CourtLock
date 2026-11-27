"""Staff-only endpoints. Identity (the staff key) is checked in api/deps.py."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from courtlock.api.deps import DbSession, require_staff
from courtlock.api.schemas import UtilizationResponse
from courtlock.domain.weeks import parse_iso_week
from courtlock.services import utilization

router = APIRouter(tags=["staff"], dependencies=[Depends(require_staff)])


@router.get("/staff/utilization", response_model=UtilizationResponse)
def get_utilization(
    session: DbSession,
    week: Annotated[str, Query(description="ISO week, e.g. 2026-W48")],
    venue_id: Annotated[uuid.UUID | None, Query()] = None,
) -> UtilizationResponse:
    """Confirmed booked hours / bookable hours for a Monday-Sunday week, per court and venue."""
    monday = parse_iso_week(week)
    return UtilizationResponse.build(
        week, monday, utilization.get_utilization(session, monday, venue_id)
    )
