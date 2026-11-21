"""Request dependencies. IDENTITY LIVES HERE AND ONLY HERE.

This is the one module that reads identity headers. Routers and services receive typed
values (a member UUID, a staff marker) and never see headers. To replace the upstream
gateway scheme with JWT/OIDC, change this file and nothing else (D-02).
"""

import hmac
import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from courtlock.config import Settings
from courtlock.errors import InvalidStaffKey, MissingIdentity


def current_member(
    x_member_id: Annotated[str | None, Header(alias="X-Member-Id")] = None,
) -> uuid.UUID:
    """The authenticated member, as asserted by the upstream gateway."""
    if x_member_id is None:
        raise MissingIdentity("missing member identity header")
    try:
        return uuid.UUID(x_member_id)
    except ValueError as exc:
        raise MissingIdentity("malformed member identity header") from exc


def require_staff(
    request: Request,
    x_staff_key: Annotated[str | None, Header(alias="X-Staff-Key")] = None,
) -> None:
    """Passes only when the staff key matches (constant-time compare)."""
    expected = request.app.state.settings.staff_api_key
    if x_staff_key is None or not hmac.compare_digest(x_staff_key.encode(), expected.encode()):
        raise InvalidStaffKey("invalid or missing staff key")


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


CurrentMember = Annotated[uuid.UUID, Depends(current_member)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[Session, Depends(get_session)]
