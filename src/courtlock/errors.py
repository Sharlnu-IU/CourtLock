"""Application errors. Each carries the HTTP status and the stable machine-readable code."""

from typing import Any


class CourtLockError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, detail: str = "", **extra: Any) -> None:
        super().__init__(detail or self.code)
        self.detail = detail or self.code
        self.extra = extra


class MissingIdentity(CourtLockError):
    status_code = 401
    code = "missing_identity"


class InvalidStaffKey(CourtLockError):
    status_code = 401
    code = "invalid_staff_key"


class MemberNotFound(CourtLockError):
    status_code = 404
    code = "member_not_found"


class CourtNotFound(CourtLockError):
    status_code = 404
    code = "court_not_found"


class VenueNotFound(CourtLockError):
    status_code = 404
    code = "venue_not_found"


class HoldNotFound(CourtLockError):
    status_code = 404
    code = "hold_not_found"


class BookingNotFound(CourtLockError):
    status_code = 404
    code = "booking_not_found"


class SlotUnavailable(CourtLockError):
    status_code = 409
    code = "slot_unavailable"


class BookingLimitReached(CourtLockError):
    status_code = 409
    code = "booking_limit_reached"


class SeriesConflict(CourtLockError):
    status_code = 409
    code = "series_conflict"


class HoldExpired(CourtLockError):
    status_code = 409
    code = "hold_expired"


class InvalidState(CourtLockError):
    status_code = 409
    code = "invalid_state"


class BookingStarted(CourtLockError):
    status_code = 409
    code = "booking_started"


class InvalidSlot(CourtLockError):
    status_code = 422
    code = "invalid_slot"


class ValidationFailed(CourtLockError):
    status_code = 422
    code = "validation_error"
