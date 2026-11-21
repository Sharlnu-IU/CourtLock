"""POST /holds and POST /holds/{id}/confirm."""

import uuid

from fastapi import APIRouter, Response

from courtlock.api.deps import CurrentMember, DbSession, SettingsDep
from courtlock.api.schemas import BookingResponse, HoldCreate, HoldResponse
from courtlock.services import bookings, holds

router = APIRouter(tags=["holds"])


@router.post("/holds", status_code=201, response_model=HoldResponse)
def post_hold(
    body: HoldCreate,
    response: Response,
    member_id: CurrentMember,
    session: DbSession,
    settings: SettingsDep,
) -> HoldResponse:
    """Reserve a slot for a short time. 201 for a new hold, 200 when the member's own live
    hold for the same slot is returned (safe retry)."""
    result = holds.create_hold(session, settings, member_id, body.court_id, body.start_at)
    if not result.created:
        response.status_code = 200
    return HoldResponse.from_booking(result.booking)


@router.post("/holds/{hold_id}/confirm", response_model=BookingResponse)
def post_confirm(
    hold_id: uuid.UUID,
    response: Response,
    member_id: CurrentMember,
    session: DbSession,
) -> BookingResponse:
    """Turn a hold into a booking. Idempotent: a replay returns the same booking."""
    result = bookings.confirm_hold(session, member_id, hold_id)
    if result.replayed:
        response.headers["Idempotent-Replay"] = "true"
    return BookingResponse.from_booking(result.booking)
