"""POST /recurring-bookings."""

from fastapi import APIRouter

from courtlock.api.deps import CurrentMember, DbSession, SettingsDep
from courtlock.api.schemas import RecurringCreate, RecurringResponse
from courtlock.services import recurring

router = APIRouter(tags=["recurring"])


@router.post("/recurring-bookings", status_code=201, response_model=RecurringResponse)
def post_recurring(
    body: RecurringCreate,
    member_id: CurrentMember,
    session: DbSession,
    settings: SettingsDep,
) -> RecurringResponse:
    """Book the same local time every week for 2-12 weeks: all occurrences or none."""
    result = recurring.create_series(
        session, settings, member_id, body.court_id, body.first_start_at, body.weeks
    )
    return RecurringResponse.from_result(result)
