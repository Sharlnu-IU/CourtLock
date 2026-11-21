"""POST /bookings/{id}/cancel."""

import uuid

from fastapi import APIRouter

from courtlock.api.deps import CurrentMember, DbSession
from courtlock.api.schemas import CancelResponse
from courtlock.services import bookings

router = APIRouter(tags=["bookings"])


@router.post("/bookings/{booking_id}/cancel", response_model=CancelResponse)
def post_cancel(
    booking_id: uuid.UUID,
    member_id: CurrentMember,
    session: DbSession,
) -> CancelResponse:
    """Cancel a booking. Full refund with at least 24 h notice, otherwise none. Idempotent."""
    booking = bookings.cancel_booking(session, member_id, booking_id)
    return CancelResponse.from_booking(booking)
