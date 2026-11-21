"""A tiny HTTP client that calls the API exactly the way the README shows."""

import uuid
from datetime import datetime
from typing import Any

import httpx

from tests.support.times import iso


class Api:
    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            limits=httpx.Limits(max_connections=300, max_keepalive_connections=50),
        )

    def hold(self, member: uuid.UUID, court: uuid.UUID, start_at: datetime) -> httpx.Response:
        return self.client.post(
            "/holds",
            headers={"X-Member-Id": str(member)},
            json={"court_id": str(court), "start_at": iso(start_at)},
        )

    def hold_raw(self, member: uuid.UUID, body: dict[str, Any]) -> httpx.Response:
        return self.client.post("/holds", headers={"X-Member-Id": str(member)}, json=body)

    def confirm(self, member: uuid.UUID, hold_id: str | uuid.UUID) -> httpx.Response:
        return self.client.post(f"/holds/{hold_id}/confirm", headers={"X-Member-Id": str(member)})

    def cancel(self, member: uuid.UUID, booking_id: str | uuid.UUID) -> httpx.Response:
        return self.client.post(
            f"/bookings/{booking_id}/cancel", headers={"X-Member-Id": str(member)}
        )

    def availability(self, court: uuid.UUID, **params: str) -> httpx.Response:
        return self.client.get(f"/courts/{court}/availability", params=params)

    def close(self) -> None:
        self.client.close()
