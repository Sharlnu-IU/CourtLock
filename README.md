# CourtLock

**Court booking backend that cannot double-book.**

A community club rents sports courts by the hour across a few venues. CourtLock lets members browse availability, hold a court briefly, confirm it, cancel it, and book it weekly. The database guarantees that no court is ever booked twice, retries never create duplicates, and a failed operation never leaves half-written state.

> **Status: week 2 complete.** The four fixed endpoints (`POST /holds`, `POST /holds/{id}/confirm`, `POST /bookings/{id}/cancel`, `GET /courts/{id}/availability`) are implemented and run under `docker compose up`, with tests for all three guarantees. This README is the **API contract**: the endpoints behave *exactly* as shown here, and CI runs every `bash` block in sections 1-4 against the running stack, so this page cannot drift from the behaviour. Sections marked **(week 3)** (recurring bookings, staff utilization, court listing) are designed but **not built yet**.

| Doc | Purpose |
|---|---|
| [DESIGN.md](DESIGN.md) | PRD: goals, business rules, scope cuts, assumptions, open questions, rejected alternatives, response to review |
| [docs/TRD.md](docs/TRD.md) | Technical design: data model, how each guarantee is enforced and tested, CI |
| `LIMITATIONS.md` | How the system can still fail (written at the end of the build) |

## The three guarantees

| # | Guarantee | Enforced by |
|---|---|---|
| G1 | A court is never double-booked, however many requests arrive at once | Postgres **exclusion constraint** on `(court, time range)` for active rows |
| G2 | Retrying a timed-out confirm never makes two bookings (or two charges) | One-row state machine, conditional `UPDATE`, `UNIQUE(booking, kind)` on the ledger |
| G3 | A failed multi-step operation leaves no partial state | One use-case = one DB transaction; proven by failure injection |

Unconfirmed holds expire after **5 minutes** and then stop blocking the slot immediately, even if nothing ever cleans them up (a database trigger and view guarantee that for every writer).

## Quick start

```bash
docker compose up --build
```

Starts Postgres, runs the migration, seeds the demo data below, and serves the API at <http://localhost:8000> (interactive docs at `/docs`). Add `-d` to run it in the background; `docker compose down -v` stops it and wipes the data.

## Seed data (demo members and courts)

Fixed ids so every example below runs as-is.

| What | Name | Id / value |
|---|---|---|
| Member | Alice | `00000000-0000-4000-8000-0000000000a1` |
| Member | Bob | `00000000-0000-4000-8000-0000000000a2` |
| Member | Carol | `00000000-0000-4000-8000-0000000000a3` |
| Member | Dave | `00000000-0000-4000-8000-0000000000a4` |
| Staff key | — | `dev-staff-key` (dev only; set `STAFF_API_KEY` to change) |
| Venue | Riverside Sports Club — zone `UTC`, open 06:00–22:00, USD: off-peak 1200, peak 2000 (cents/hour). Peak: Mon–Fri 17:00–21:00, Sat–Sun 08:00–20:00 | `00000000-0000-4000-8000-0000000000b1` |
| Court | Riverside **Court 1** (tennis) | `00000000-0000-4000-8000-0000000000c1` |
| Court | Riverside **Court 2** (badminton) | `00000000-0000-4000-8000-0000000000c2` |
| Venue | Hilltop Courts — zone `Asia/Kolkata`, open 06:00–22:00 IST, INR: off-peak 40000, peak 60000. Peak: Mon–Fri 18:00–21:00, Sat–Sun 07:00–20:00 | `00000000-0000-4000-8000-0000000000b2` |
| Court | Hilltop **Court A** (basketball) | `00000000-0000-4000-8000-0000000000c3` |

## Conventions

- **Requests/responses** are JSON (`Content-Type: application/json` on POST bodies). Field names are `snake_case`.
- **Identity** is supplied by an upstream gateway: send `X-Member-Id: <member uuid>` on member calls and `X-Staff-Key: <key>` on staff calls. (Login is out of scope. All header handling lives in one source file, `api/deps.py`.) Availability needs no header.
- **Times** in requests are ISO-8601 *with an offset* (`2026-12-01T10:00:00Z` or `2026-12-01T15:30:00+05:30`). Responses are always UTC (`Z`). A slot is **one hour**, starting on the hour **in the venue's local time** (so at Hilltop, slots start at :30 UTC).
- **Money** is integer minor units (cents) plus a `currency` code.
- **Errors** are `application/problem+json`, e.g. `{"type":"about:blank","title":"Conflict","status":409,"code":"slot_unavailable","detail":"…"}`. Switch on `code`, not on the text. Full table at the end.
- Examples need `curl` and `jq`. Use **bash** (Linux, WSL, Git Bash; on macOS install coreutils and use `gdate` wherever `date -d` appears). In Windows PowerShell call `curl.exe`, not `curl`.
- Response bodies below are **illustrative** (ids, dates and the peak/off-peak band depend on when you run them).

### Set up variables (run once per shell)

```bash
BASE=http://localhost:8000
ALICE=00000000-0000-4000-8000-0000000000a1
BOB=00000000-0000-4000-8000-0000000000a2
COURT=00000000-0000-4000-8000-0000000000c1      # Riverside Court 1
STAFF_KEY=dev-staff-key

# A slot two days from now at 10:00 UTC (inside Riverside's opening hours, > 24 h away).
START=$(date -u -d '+2 days' +%Y-%m-%dT10:00:00Z)     # Linux / WSL / Git Bash
# macOS instead:  START=$(date -u -v+2d +%Y-%m-%dT10:00:00Z)
DAY=${START%%T*}
```

---

## 1. Check availability — `GET /courts/{court_id}/availability`

No auth. Query: `from` (venue-local date, default today), `to` (venue-local date, **exclusive**, default `from` + 1 day). At most 14 days. Slots that have already started are omitted. `status` is `AVAILABLE`, `HELD` (someone is mid-checkout; may free up) or `BOOKED`. An expired hold shows as `AVAILABLE`. Availability is **advisory** — only a hold reserves a slot.

```bash
curl -s "$BASE/courts/$COURT/availability?from=$DAY" | jq
```

`200 OK`

```json
{
  "court_id": "00000000-0000-4000-8000-0000000000c1",
  "timezone": "UTC",
  "slots": [
    {"start_at": "2026-12-01T06:00:00Z", "end_at": "2026-12-01T07:00:00Z",
     "status": "AVAILABLE", "price_band": "OFFPEAK", "price_cents": 1200, "currency": "USD"},
    {"start_at": "2026-12-01T07:00:00Z", "end_at": "2026-12-01T08:00:00Z",
     "status": "AVAILABLE", "price_band": "OFFPEAK", "price_cents": 1200, "currency": "USD"}
  ]
}
```

Errors: `404 court_not_found`, `422 validation_error` (bad date, span > 14 days).

## 2. Hold a slot — `POST /holds`

Reserves the slot for **5 minutes**. Header `X-Member-Id` required. The price is **locked now**.

```bash
curl -s -X POST "$BASE/holds" \
  -H "Content-Type: application/json" \
  -H "X-Member-Id: $ALICE" \
  -d "{\"court_id\": \"$COURT\", \"start_at\": \"$START\"}" | jq
```

`201 Created`

```json
{
  "id": "5f0c3c1e-7a3b-4c52-9d0e-2b6a1d4e8f10",
  "court_id": "00000000-0000-4000-8000-0000000000c1",
  "member_id": "00000000-0000-4000-8000-0000000000a1",
  "start_at": "2026-12-01T10:00:00Z",
  "end_at": "2026-12-01T11:00:00Z",
  "status": "HELD",
  "price_band": "OFFPEAK",
  "price_cents": 1200,
  "currency": "USD",
  "expires_at": "2026-11-29T10:05:00Z"
}
```

- **Safe to retry:** if the same member repeats the call for the same slot while their hold is still live, they get the **same hold back** with `200 OK` (not an error).
- A member can have at most **3 active future bookings** (live holds count; a whole recurring series counts as one).

| Status | `code` | When |
|---|---|---|
| 401 | `missing_identity` | no `X-Member-Id` |
| 404 | `member_not_found`, `court_not_found` | unknown ids |
| 409 | `slot_unavailable` | someone else holds or has booked it |
| 409 | `booking_limit_reached` | member already has 3 active future bookings |
| 422 | `invalid_slot` | not on the hour in the venue's time, outside opening hours, in the past, or more than 90 days ahead |
| 422 | `validation_error` | malformed body |

Save the hold id for the next step:

```bash
HOLD=$(curl -s -X POST "$BASE/holds" -H "Content-Type: application/json" -H "X-Member-Id: $ALICE" \
  -d "{\"court_id\": \"$COURT\", \"start_at\": \"$START\"}" | jq -r .id)
echo "$HOLD"
```

### Try the double-booking guarantee

While Alice's hold is live, Bob asks for the same slot:

```bash
curl -s -X POST "$BASE/holds" \
  -H "Content-Type: application/json" \
  -H "X-Member-Id: $BOB" \
  -d "{\"court_id\": \"$COURT\", \"start_at\": \"$START\"}" | jq
```

`409 Conflict`

```json
{"type": "about:blank", "title": "Conflict", "status": 409,
 "code": "slot_unavailable", "detail": "That slot is held or booked."}
```

## 3. Confirm — `POST /holds/{hold_id}/confirm`

Turns the hold into a booking. No body. Only the member who made the hold can confirm it. The booking **id is the hold id**. Charges exactly the locked price.

```bash
curl -s -X POST "$BASE/holds/$HOLD/confirm" -H "X-Member-Id: $ALICE" | jq
```

`200 OK`

```json
{
  "id": "5f0c3c1e-7a3b-4c52-9d0e-2b6a1d4e8f10",
  "court_id": "00000000-0000-4000-8000-0000000000c1",
  "member_id": "00000000-0000-4000-8000-0000000000a1",
  "status": "CONFIRMED",
  "confirmed_at": "2026-11-29T10:01:12Z",
  "start_at": "2026-12-01T10:00:00Z",
  "end_at": "2026-12-01T11:00:00Z",
  "price_band": "OFFPEAK",
  "price_cents": 1200,
  "currency": "USD"
}
```

**Safe to retry.** If the request times out, send it again, any number of times, even concurrently. You always get `200` with the **same** booking, and you are charged **once**. A replay carries the response header `Idempotent-Replay: true`:

```bash
curl -s -i -X POST "$BASE/holds/$HOLD/confirm" -H "X-Member-Id: $ALICE" | head -n 12
```

| Status | `code` | When |
|---|---|---|
| 401 | `missing_identity` | no `X-Member-Id` |
| 404 | `hold_not_found` | no such hold, **or it belongs to another member** |
| 409 | `hold_expired` | more than 5 minutes passed (hold again) |
| 409 | `invalid_state` | the booking was cancelled |

## 4. Cancel — `POST /bookings/{booking_id}/cancel`

No body. Only the booking's owner. **Refund rule:** if the slot starts **at least 24 hours** from now → **100 %** refund; otherwise **0 %**. A booking that has already started cannot be cancelled. The slot becomes bookable again immediately either way. Cancelling a **live, unconfirmed hold** simply releases it (nothing was charged: refund 0).

```bash
curl -s -X POST "$BASE/bookings/$HOLD/cancel" -H "X-Member-Id: $ALICE" | jq
```

`200 OK` (the slot above is > 24 h away → full refund)

```json
{
  "id": "5f0c3c1e-7a3b-4c52-9d0e-2b6a1d4e8f10",
  "status": "CANCELLED",
  "cancelled_at": "2026-11-29T10:03:40Z",
  "price_cents": 1200,
  "refund_percent": 100,
  "refund_cents": 1200,
  "currency": "USD"
}
```

**Safe to retry:** cancelling again returns the **same stored result** (same `cancelled_at`, same refund) and never refunds twice.

A late cancellation (less than 24 h before the start) returns `"refund_percent": 0, "refund_cents": 0`. To see it, book a slot that is only hours away. Riverside is open 06:00–22:00 UTC, so this picks the first open hour at least ~2 h from now (always < 24 h away):

```bash
T=$(date -u -d '+2 hours' +%Y-%m-%dT%H:00:00Z); H=$((10#${T:11:2})); D=${T%%T*}
if   [ "$H" -lt 6 ];  then SOON=${D}T06:00:00Z
elif [ "$H" -ge 22 ]; then SOON=$(date -u -d "$D +1 day" +%Y-%m-%dT06:00:00Z)
else SOON=$T; fi
LATE=$(curl -s -X POST "$BASE/holds" -H "Content-Type: application/json" -H "X-Member-Id: $ALICE" \
  -d "{\"court_id\": \"$COURT\", \"start_at\": \"$SOON\"}" | jq -r .id)
curl -s -X POST "$BASE/holds/$LATE/confirm" -H "X-Member-Id: $ALICE" > /dev/null
curl -s -X POST "$BASE/bookings/$LATE/cancel" -H "X-Member-Id: $ALICE" | jq
```

| Status | `code` | When |
|---|---|---|
| 401 | `missing_identity` | no `X-Member-Id` |
| 404 | `booking_not_found` | no such booking, or not yours |
| 409 | `booking_started` | the slot has already started |
| 409 | `hold_expired` | the id is an expired hold |

---

## 5. Weekly recurring booking — `POST /recurring-bookings` (week 3)

Books the same **local** time every 7 days for 2–12 weeks, **all or nothing**: if any week is unavailable, nothing is booked. Charged up front, one charge per occurrence. The whole series counts as **one** booking toward the member's cap of 3. Each occurrence can be cancelled separately with `POST /bookings/{id}/cancel` (refund by the 24 h rule).

```bash
CAROL=00000000-0000-4000-8000-0000000000a3
COURT2=00000000-0000-4000-8000-0000000000c2
FIRST=$(date -u -d '+10 days' +%Y-%m-%dT11:00:00Z)   # macOS: date -u -v+10d +%Y-%m-%dT11:00:00Z
curl -s -X POST "$BASE/recurring-bookings" \
  -H "Content-Type: application/json" \
  -H "X-Member-Id: $CAROL" \
  -d "{\"court_id\": \"$COURT2\", \"first_start_at\": \"$FIRST\", \"weeks\": 12}" | jq
```

`201 Created`

```json
{
  "series_id": "9a1d7a52-0c4e-4a0b-8b8e-6d1f3c2e7a44",
  "court_id": "00000000-0000-4000-8000-0000000000c2",
  "member_id": "00000000-0000-4000-8000-0000000000a3",
  "weeks": 12,
  "total_price_cents": 14400,
  "currency": "USD",
  "bookings": [
    {"id": "…", "start_at": "2026-12-10T11:00:00Z", "end_at": "2026-12-10T12:00:00Z",
     "status": "CONFIRMED", "price_band": "OFFPEAK", "price_cents": 1200}
  ]
}
```

(`bookings` has one entry per week.)

| Status | `code` | When |
|---|---|---|
| 409 | `series_conflict` | at least one week is taken. Body includes `"conflicts": [{"start_at": "…"}]` and **nothing was booked**. Expired, unswept holds do **not** count as conflicts |
| 409 | `booking_limit_reached` | member already has 3 active future bookings/series |
| 422 | `invalid_slot` | any occurrence not on a venue-local hour, outside opening hours, first slot in the past or > 90 days away; `weeks` outside 2–12 |

## 6. Weekly utilization (staff) — `GET /staff/utilization` (week 3)

Query: `week` (ISO week, e.g. `2026-W48`, Monday–Sunday in each venue's local time), optional `venue_id`. Header `X-Staff-Key`. Utilization = confirmed booked hours ÷ bookable (opening) hours. Cancelled bookings and holds are not counted.

```bash
curl -s "$BASE/staff/utilization?week=2026-W48" -H "X-Staff-Key: $STAFF_KEY" | jq
```

`200 OK`

```json
{
  "week": "2026-W48",
  "week_start": "2026-11-23",
  "week_end": "2026-11-30",
  "venues": [
    {"venue_id": "00000000-0000-4000-8000-0000000000b1", "name": "Riverside Sports Club",
     "timezone": "UTC", "booked_hours": 31, "bookable_hours": 224, "utilization_pct": 13.8,
     "courts": [
       {"court_id": "00000000-0000-4000-8000-0000000000c1", "name": "Court 1",
        "booked_hours": 20, "bookable_hours": 112, "utilization_pct": 17.9,
        "peak_hours": 9, "offpeak_hours": 11}
     ]}
  ]
}
```

`week_end` is exclusive. Errors: `401 invalid_staff_key`, `422 validation_error`.

## 7. Browse courts — `GET /courts` (week 3, optional)

No auth. Lists active courts so a client can discover ids.

```bash
curl -s "$BASE/courts" | jq
```

`200 OK`: `{"courts": [{"id": "…", "name": "Court 1", "sport": "tennis", "venue_id": "…", "venue_name": "Riverside Sports Club", "timezone": "UTC"}]}`

---

## Error codes

| Status | Codes |
|---|---|
| 401 | `missing_identity`, `invalid_staff_key` |
| 404 | `member_not_found`, `court_not_found`, `hold_not_found`, `booking_not_found` |
| 409 | `slot_unavailable`, `booking_limit_reached`, `hold_expired`, `invalid_state`, `booking_started`, `series_conflict` |
| 422 | `invalid_slot`, `validation_error` |
| 500 | `internal_error` (never exposes SQL) |

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | set by compose | Postgres connection |
| `STAFF_API_KEY` | `dev-staff-key` | value of `X-Staff-Key` |
| `HOLD_TTL_SECONDS` | `300` | how long an unconfirmed hold lasts |
| `MAX_ACTIVE_BOOKINGS` | `3` | per-member cap |
| `BOOKING_HORIZON_DAYS` | `90` | how far ahead the first slot may be |

## Development

```bash
uv sync                                  # install (Python 3.11+); uv: https://docs.astral.sh/uv/
docker compose up -d db                  # Postgres on 127.0.0.1:5432 for the tests
uv run pytest                            # unit, integration, concurrency, atomicity, contract
uv run ruff check . && uv run ruff format --check .
uv run mypy                              # strict; zero inline suppressions
```

- The tests create their own `courtlock_test` database and build it by running the real Alembic migration. Point them elsewhere with `TEST_DATABASE_URL`.
- Run the suite on Python 3.11 exactly as CI does: `docker compose --profile test run --rm test`.
- Check the README against a running stack (needs `bash`, `curl`, `jq`): `python scripts/readme_check.py`.
- On Windows, use `127.0.0.1` rather than `localhost` for the database URL: `localhost` tries IPv6 first and each connection stalls for ~8 seconds.

CI (`.github/workflows/ci.yml`) runs ruff, ruff format, mypy, a grep that fails on any `noqa` / `type: ignore`, the full test suite against a `postgres:15` service, then `docker compose up` and the README's curl examples.

```
src/courtlock/
  api/       routers, schemas, errors, deps.py (identity: the only place headers are read)
  services/  use-cases, one DB transaction each
  domain/    pure rules: slots, pricing, refund, weeks
  db/        models, session, deadlock retry
tests/       unit · integration · concurrency · atomicity · contract
```

## Open questions

The brief is intentionally incomplete; every gap has a documented decision. The ones most worth a client conversation are in [DESIGN.md §11](DESIGN.md), notably how a recurring series should count toward the 3-booking cap (we count it **once**, which lets a member hold up to 36 future slot-hours; see DESIGN.md §14 for the cost).
