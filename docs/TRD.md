# CourtLock — Technical Requirements Document (TRD)

| | |
|---|---|
| **Status** | Draft v0.1 — design only, no code written yet |
| **Companion** | [DESIGN.md](../DESIGN.md) (PRD: business rules `BR-*`, requirements `FR-*`/`NFR-*`, open questions `Q-*`) |

This document says *how* we make the three guarantees true:

- **G1** a court is never double-booked, however many requests arrive at once
- **G2** a client retrying a timed-out confirm never ends up with two bookings
- **G3** a failed multi-step operation leaves no partial state

**Design principle:** every guarantee is enforced by **Postgres** (constraints, row locks, transactions), not by Python code, locks or caches. Application code is allowed to be wrong and concurrent; the database refuses to store a bad state. That is what makes the guarantees hold with several API instances (NFR-2) and what makes them provable by test.

---

## 1. Architecture

```
          ┌────────────┐   HTTP    ┌────────────────────┐        ┌──────────────┐
 client ─▶│  gateway   │──────────▶│  api (FastAPI) ×N  │───────▶│              │
 (auth)   │ (upstream) │ X-Member- │  stateless         │  SQL   │ Postgres 15  │
          └────────────┘   Id      └────────────────────┘        │  (source of  │
                                    ┌────────────────────┐        │   truth)     │
                                    │ worker ×1+         │───────▶│              │
                                    │ expiry sweeper,    │        └──────────────┘
                                    │ key purge          │
                                    └────────────────────┘
```

- **api** — stateless FastAPI processes. Any number can run.
- **worker** — one small process that tidies up (marks expired holds, purges old idempotency keys). **Nothing depends on it for correctness** (§6.2). Safe to run several (`SKIP LOCKED`).
- **db** — Postgres 15 with the `btree_gist` extension.
- `migrate` — one-shot compose service running `alembic upgrade head` (+ seed in dev) before api/worker start.

Layering inside the api (dependencies point downward only):

```
api/       routers, request/response schemas, error handlers, identity dependencies
services/  use-cases: one function = one DB transaction (hold, confirm, cancel, recurring, availability, utilization)
domain/    pure functions, no I/O: slots, pricing, refund policy, quota policy, week maths
db/        SQLAlchemy models, session/unit-of-work, retry helper
```

`domain/` being pure is deliberate: the money-and-time rules (refund boundary, DST, peak windows) are unit-testable without a database.

## 2. Stack & tooling

| Concern | Choice |
|---|---|
| Language / runtime | Python 3.11 |
| Web | FastAPI + Uvicorn, **sync** endpoints (run in Starlette's threadpool) |
| ORM / driver | SQLAlchemy 2.0 (typed `Mapped[]` models, 2.0 style) + **psycopg 3** |
| Migrations | Alembic; migrations are hand-reviewed, including raw DDL for the exclusion constraint |
| DB | Postgres 15 (`postgres:15` image; `btree_gist` is in contrib) |
| Settings | `pydantic-settings` (env vars) |
| Tests | pytest, pytest-xdist off for concurrency tests, `httpx` + FastAPI `TestClient`, real Postgres (compose service locally; service container in CI) |
| Lint / format | `ruff check` + `ruff format --check` |
| Types | `mypy --strict` |
| Packaging | `pyproject.toml` + `uv` (lockfile committed) |
| Suppressions | **None.** No `# noqa`, `# type: ignore`, `# pyright:`, `# fmt:`. CI greps for them and fails. Rule selection lives in `pyproject.toml` (config, not inline), and we do not use `per-file-ignores` to hide real findings. |

Why sync, not async: the work is DB-bound; async adds a second concurrency model (and mypy/typing friction with async SQLAlchemy) while the guarantees come from the DB anyway. Pool sizing is explicit (§11).

## 3. Repository layout (planned)

```
.
├── README.md  DESIGN.md  LIMITATIONS.md (later)
├── docs/TRD.md
├── docker-compose.yml  Dockerfile
├── pyproject.toml  uv.lock  alembic.ini
├── alembic/versions/…
├── src/courtlock/
│   ├── main.py  config.py  clock.py
│   ├── api/        (routers/, schemas.py, errors.py, deps.py)
│   ├── services/   (holds.py, bookings.py, recurring.py, availability.py, utilization.py, idempotency.py)
│   ├── domain/     (slots.py, pricing.py, refund.py, quota.py, weeks.py)
│   ├── db/         (models.py, session.py, uow.py, retry.py)
│   ├── worker.py   seed.py
├── tests/
│   ├── unit/  integration/  concurrency/  atomicity/  contract/
└── .github/workflows/ci.yml
```

## 4. Data model

All ids are UUID (generated app-side). All timestamps are `timestamptz` (UTC). Money is `integer` minor units.

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TYPE booking_status AS ENUM ('HELD','CONFIRMED','CANCELLED','EXPIRED');
CREATE TYPE price_band     AS ENUM ('PEAK','OFFPEAK');

venues(
  id uuid PK, name text NOT NULL,
  timezone text NOT NULL,                       -- IANA, e.g. 'Asia/Kolkata'
  opens_at time NOT NULL, closes_at time NOT NULL,   -- local; CHECK opens_at < closes_at
  peak_price_cents int NOT NULL CHECK (>= 0),
  offpeak_price_cents int NOT NULL CHECK (>= 0),
  currency char(3) NOT NULL )

peak_windows(
  id uuid PK, venue_id uuid FK,
  weekday smallint CHECK (0..6),                -- 0 = Monday, venue-local
  start_time time, end_time time CHECK (start_time < end_time) )

courts(
  id uuid PK, venue_id uuid FK, name text, sport text,
  is_active bool NOT NULL DEFAULT true, UNIQUE (venue_id, name) )

members( id uuid PK, name text NOT NULL, email text NOT NULL UNIQUE )

recurring_series(
  id uuid PK, member_id FK, court_id FK,
  weekday smallint, local_start_time time, occurrences smallint CHECK (2..12),
  created_at timestamptz )

bookings(                                       -- a HOLD is a booking in status HELD
  id uuid PK,                                   -- the hold id IS the booking id
  member_id uuid FK NOT NULL, court_id uuid FK NOT NULL,
  series_id uuid FK NULL,
  start_at timestamptz NOT NULL, end_at timestamptz NOT NULL,
  during tstzrange GENERATED ALWAYS AS (tstzrange(start_at, end_at, '[)')) STORED,
  status booking_status NOT NULL,
  band price_band NOT NULL, price_cents int NOT NULL CHECK (>= 0), currency char(3) NOT NULL,
  hold_expires_at timestamptz NULL,             -- required while HELD
  confirmed_at timestamptz NULL, cancelled_at timestamptz NULL,
  refund_cents int NULL,                        -- set exactly once, at cancel
  created_at timestamptz NOT NULL,
  CHECK (end_at = start_at + interval '1 hour'),
  CHECK (status <> 'HELD' OR hold_expires_at IS NOT NULL),
  CHECK (status <> 'CONFIRMED' OR confirmed_at IS NOT NULL),
  CHECK (status <> 'CANCELLED' OR (cancelled_at IS NOT NULL AND refund_cents IS NOT NULL)),

  -- ★ THE double-booking guarantee (G1) ★
  EXCLUDE USING gist (court_id WITH =, during WITH &&)
          WHERE (status IN ('HELD','CONFIRMED')) )

CREATE INDEX bookings_member_active_idx ON bookings (member_id, start_at)
        WHERE status IN ('HELD','CONFIRMED');
CREATE INDEX bookings_held_expiry_idx   ON bookings (hold_expires_at) WHERE status = 'HELD';
CREATE INDEX bookings_court_time_idx    ON bookings (court_id, start_at);

ledger_entries(                                 -- append-only money log
  id uuid PK, booking_id uuid FK NOT NULL, member_id uuid FK NOT NULL,
  kind text CHECK (kind IN ('CHARGE','REFUND')),
  amount_cents int NOT NULL CHECK (> 0), currency char(3), created_at timestamptz,
  UNIQUE (booking_id, kind) )                   -- ★ at most one charge and one refund per booking (G2) ★

idempotency_keys(
  member_id uuid, key text, endpoint text, request_hash text,
  status_code smallint, response_body jsonb, created_at timestamptz,
  PRIMARY KEY (member_id, key) )
```

Why this shape:

- **One `bookings` table for holds and bookings.** Confirm is a state change on one row. There is no moment where the slot is "released by the hold and not yet taken by the booking" — the classic double-booking window of two-table designs.
- **`EXCLUDE … WHERE status IN ('HELD','CONFIRMED')`** — two active rows for the same court with overlapping time ranges cannot exist. `CANCELLED`/`EXPIRED` rows drop out of the constraint, which is how a slot is freed. The constraint also covers future multi-hour bookings (Q-4) with no change.
- **The constraint cannot mention `now()`** (index predicates must be immutable). That is why expiry needs the explicit step in §6.2.
- **`UNIQUE (booking_id, kind)` on the ledger** makes a double charge or double refund *structurally impossible*, even if application logic were wrong.
- Slot alignment (start on the hour in *venue* local time) is validated in `domain/slots.py`, not in SQL, because it depends on the venue's time zone (including half-hour-offset zones like IST).

## 5. API specification

Conventions: JSON, `snake_case`, timestamps are ISO-8601 with offset (responses always UTC `Z`). No path version prefix (the contract paths are fixed). Errors are `application/problem+json` (§8).

**Identity (decision D-02; Q-9):**
`X-Member-Id: <uuid>` on member endpoints; `X-Staff-Key: <secret>` on `/staff/*` (constant-time compare against `STAFF_API_KEY`). Missing/unknown → 401 / 404 as listed in §8. All of this lives in `api/deps.py`, so changing the identity scheme touches one file.

### 5.1 `POST /holds`

Headers: `X-Member-Id` (required), `Idempotency-Key` (optional, FR-12).

```json
{ "court_id": "uuid", "start_at": "2026-11-20T18:00:00+05:30" }
```

`201 Created`

```json
{ "id": "uuid", "court_id": "uuid", "member_id": "uuid",
  "start_at": "2026-11-20T12:30:00Z", "end_at": "2026-11-20T13:30:00Z",
  "status": "HELD", "price_band": "PEAK", "price_cents": 1200, "currency": "INR",
  "expires_at": "2026-11-09T10:05:00Z" }
```

Errors: `422 invalid_slot` (not on the hour in venue time / outside opening hours / in the past / beyond horizon), `404 court_not_found`, `409 slot_unavailable`, `409 booking_limit_reached`.

### 5.2 `POST /holds/{id}/confirm`

No body. Headers: `X-Member-Id`.

`200 OK` — first call **and** every replay:

```json
{ "id": "<same id as the hold>", "status": "CONFIRMED", "confirmed_at": "…",
  "court_id": "…", "start_at": "…", "end_at": "…",
  "price_band": "PEAK", "price_cents": 1200, "currency": "INR" }
```

Replay adds response header `Idempotent-Replay: true`. Errors: `404 hold_not_found` (also for another member's hold — no id probing), `409 hold_expired`, `409 invalid_state` (cancelled/…), `409 slot_started`.

### 5.3 `POST /bookings/{id}/cancel`

No body. Headers: `X-Member-Id`.

`200 OK`

```json
{ "id": "…", "status": "CANCELLED", "cancelled_at": "…",
  "refund_cents": 1200, "refund_percent": 100, "currency": "INR" }
```

`refund_percent` is `100` if `start_at − now ≥ 24h` else `0`. Replay returns the **stored** values (same `cancelled_at`, same refund). Errors: `404 booking_not_found`, `409 booking_started`, `409 invalid_state` (still a HELD hold → use `DELETE /holds/{id}`; EXPIRED).

### 5.4 `GET /courts/{id}/availability?from=2026-11-20&to=2026-11-22`

`from`/`to` are **venue-local dates**, `to` exclusive, default `to = from + 1 day`, max span 14 days, `from` defaults to today.

```json
{ "court_id": "…", "timezone": "Asia/Kolkata",
  "slots": [
    { "start_at": "2026-11-20T00:30:00Z", "end_at": "2026-11-20T01:30:00Z",
      "status": "AVAILABLE", "price_band": "OFFPEAK", "price_cents": 600, "currency": "INR" },
    { "start_at": "…", "status": "HELD", "…": "…" },
    { "start_at": "…", "status": "BOOKED", "…": "…" } ] }
```

Slots already started are omitted. Expired holds are reported `AVAILABLE` (the read applies the same predicate as §6.2). No member identity is exposed (BR-20).

### 5.5 Other endpoints (design in brief)

| Endpoint | Notes |
|---|---|
| `POST /recurring-bookings` | `{court_id, weekday, local_start_time, weeks}` (or first `start_at` + `weeks`). `201` with the series and its bookings, or `409` listing **every** conflicting occurrence date (`conflicts: [...]`) and nothing is written. |
| `POST /recurring-bookings/{id}/cancel` | Cancels all future occurrences in one transaction. |
| `DELETE /holds/{id}` | Owner releases an unexpired hold → status `CANCELLED`, refund 0, no ledger entries. |
| `GET /me/bookings?status=…` | Own rows only. |
| `GET /venues`, `GET /venues/{id}/courts` | Public browse. |
| `GET /staff/utilization?week=2026-W47&venue_id=` | §7.4. |
| `GET /healthz`, `GET /readyz` | Liveness / DB-reachable. |

## 6. Concurrency & correctness design

### 6.1 G1 — never double-booked

The only way to create an active row is `INSERT INTO bookings (…status 'HELD'…)` (holds) or the batch insert (recurring). Both go through the exclusion constraint.

- Two transactions inserting overlapping ranges on one court: the second **blocks** on the first's uncommitted row; if the first commits, the second fails with SQLSTATE `23P01` (`exclusion_violation`), which we map to `409 slot_unavailable`; if the first rolls back, the second succeeds. This is true at the default `READ COMMITTED` isolation.
- Application does **not** check-then-insert as its defence. (We may run a cheap pre-check to return nicer errors, but the constraint is the authority.)
- Even code that bypasses the API (a rogue script, a future endpoint, a bug) cannot violate G1.

**Proof by test:** §10 — N concurrent HTTP requests + a direct-SQL attack on the constraint + a randomized property test comparing against a model.

### 6.2 Holds that never block forever (BR-6)

The constraint treats `HELD` as blocking, but `now()` cannot be in an index predicate. So expiry is done in **the same transaction as every hold insert**, before the insert:

```sql
-- in create_hold(), same transaction, before INSERT
UPDATE bookings SET status = 'EXPIRED'
 WHERE court_id = :court AND during && :slot AND status = 'HELD' AND hold_expires_at <= :now;
INSERT INTO bookings (...) VALUES (...);          -- may now succeed because the stale hold is no longer HELD
```

- Correctness does **not** depend on the worker. If the worker is dead for a week, an expired hold still cannot block anyone: the next person who wants that slot evicts it as part of their own transaction.
- The same predicate (`status='HELD' AND hold_expires_at > now`) is used when reading availability and when counting a member's cap, so those reads treat expired holds as free without writing.
- The worker's sweep (`UPDATE … SET status='EXPIRED' WHERE status='HELD' AND hold_expires_at <= now`, in batches with `FOR UPDATE SKIP LOCKED`) is housekeeping so that rows reach a terminal state and reports are tidy.

Races (all resolved by row locks, READ COMMITTED re-evaluation):

| Race | Outcome |
|---|---|
| confirm(H) vs. new hold for same slot evicting H, H expired | whichever locks row H first; if eviction wins, confirm sees `EXPIRED` → `hold_expired`; if confirm runs first it also sees `now ≥ expires` → `hold_expired`. Never both succeed. |
| confirm(H) just **before** expiry vs. new hold | confirm locks H, sets `CONFIRMED`. The evicting `UPDATE` re-checks its `WHERE` after the lock is released, no longer matches, evicts nothing; the INSERT then fails the constraint → `slot_unavailable`. |

### 6.3 G2 — retried confirm (and cancel, and hold)

**Confirm** is idempotent *by construction*: it is a state transition on one row identified by the hold id.

```
BEGIN;
SELECT * FROM bookings WHERE id=:id AND member_id=:me FOR UPDATE;   -- serialises concurrent confirms
  not found                 → 404
  status = CONFIRMED        → return it (replay)        -- no new charge
  status = HELD, now ≥ exp  → mark EXPIRED, 409 hold_expired   (committed)
  status = HELD, now < exp  → UPDATE status=CONFIRMED, confirmed_at=:now
                              INSERT ledger_entries(CHARGE, price_cents)     -- UNIQUE(booking_id,kind)
  else                      → 409 invalid_state
COMMIT;
```

A client that times out and retries while the first attempt is still running simply queues on the row lock, then sees `CONFIRMED` and gets the same body. No second booking exists because there is only one row; no second charge can exist because of the unique index.

Note one subtlety: the "expired" branch writes `EXPIRED` and must commit, so that error path is deliberately *not* rolled back; it is a single-row, self-consistent state change.

**Cancel** uses the same pattern (`FOR UPDATE`; `CANCELLED` → return stored refund; else compute refund, write `refund_cents`, insert `REFUND` ledger row if `> 0`).

**Hold creation** is the one call that is not naturally idempotent: if the response to `POST /holds` is lost, the retry hits the member's own hold on that slot and gets a confusing `409 slot_unavailable`, even though the first attempt succeeded. `Idempotency-Key` closes that gap: inside the same transaction we `INSERT INTO idempotency_keys … ON CONFLICT DO NOTHING`. If we inserted, we proceed and store the response before commit. If it already exists (even uncommitted — Postgres waits for the other transaction), we return the stored response. A mismatched `request_hash` returns `422 idempotency_key_reuse`. Because the key row is in the same transaction as the work, a failed attempt stores nothing and may be retried freely.

### 6.4 G3 — no partial state

- **One use-case = one DB transaction** (`with uow:`). Nothing is committed until the function returns; any exception → rollback of *everything* (booking rows, ledger rows, idempotency key).
- **No external side effects inside a transaction.** v1 has none: payments are ledger rows (D-07), no emails, no HTTP calls. If we ever add them, they go through a transactional outbox (§12).
- Multi-step operations and their atomic units:

| Operation | Steps in the one transaction |
|---|---|
| confirm | status → CONFIRMED, charge ledger row |
| cancel | status → CANCELLED, refund amount, refund ledger row |
| recurring create | series row, N booking rows, N charge rows, idempotency key |
| recurring cancel | N cancellations, ≤ N refund rows |
| create hold | evict expired, quota check, insert, idempotency key |

- **Proof by fault injection:** services call `faults.checkpoint("after_status_update")` at named points. Production wiring is a no-op object; tests inject one that raises at a chosen point. For every operation × every checkpoint the test asserts that a full snapshot of all tables is **identical** before and after. This avoids inline test hooks in production code paths (no `if TESTING`).

### 6.5 Member cap under concurrency (BR-10)

Two simultaneous holds by one member at count = 2 would each see "2 < 3". We serialise per member:

```sql
SELECT id FROM members WHERE id = :me FOR UPDATE;      -- first statement of hold & recurring
SELECT count(*) FROM bookings
 WHERE member_id=:me AND start_at > :now
   AND (status='CONFIRMED' OR (status='HELD' AND hold_expires_at > :now));
```

Requests by *different* members never contend on this lock. Confirm and cancel need no cap logic (a hold already counted).

**Lock order** (to avoid deadlocks): `members` row → (idempotency key) → booking rows inserted in ascending `(court_id, start_at)`. Even so, two recurring requests by different members over interleaving slots can deadlock; Postgres aborts one with `40P01`. `db/retry.py` retries the whole use-case up to 3 times with jitter on `40P01` and `40001`. Because the use-case is one transaction, retry is safe.

### 6.6 Isolation level

`READ COMMITTED` plus constraints and targeted `FOR UPDATE`. We rejected `SERIALIZABLE` as the primary mechanism (§16): it would also be correct but turns ordinary contention into retry storms and is harder to reason about from the outside. Constraint-first is simpler to prove.

## 7. Domain logic (pure, in `domain/`)

### 7.1 Slots (BR-1, 3, 4)

`slots_for(venue, local_date)` builds each slot as a zone-aware local datetime at `opens_at … closes_at` in 1-hour steps, converts to UTC. DST: a non-existent local hour (spring-forward gap) is skipped; an ambiguous hour (fall-back) uses the first occurrence (`fold=0`). `validate_slot(venue, start_at, now)` rejects: not exactly an hour boundary in venue-local time, outside opening hours, `start_at ≤ now`, `start_at > now + horizon`.

### 7.2 Pricing (BR-8, 9)

`price_for(venue, start_at)` → `(band, cents)`. Convert to venue-local; peak if `weekday` has a window with `start_time ≤ local_time < end_time`. Output is snapshotted onto the booking row at hold time; confirm never recomputes.

### 7.3 Refund (BR-11)

```python
def refund_cents(price_cents: int, start_at: datetime, now: datetime) -> int:
    return price_cents if start_at - now >= timedelta(hours=24) else 0
```

Unit-tested at 24h+1s, exactly 24h, 24h−1s, and after start (the last is rejected earlier, BR-12).

### 7.4 Utilization (BR-18, FR-6)

Week = ISO week, Monday 00:00 to next Monday 00:00 in venue-local time. 

- `bookable_hours(court, week)` = count of slots from `slots_for` over the 7 local dates (so DST weeks are right).
- `booked_hours` = SQL: `SELECT court_id, band, count(*) FROM bookings WHERE status='CONFIRMED' AND start_at >= :week_start AND start_at < :week_end GROUP BY court_id, band`.
- Response: per court `{booked_hours, bookable_hours, utilization_pct, peak_hours, offpeak_hours}` and a venue total. Inactive courts are excluded from the denominator.

## 8. Error model

`application/problem+json`: `{ "type": "…", "title": "…", "status": 409, "code": "slot_unavailable", "detail": "…", "request_id": "…" }`. `code` is the stable, machine-readable field; clients/tests should switch on it.

| Status | Codes |
|---|---|
| 401 | `missing_identity`, `invalid_staff_key` |
| 404 | `court_not_found`, `hold_not_found`, `booking_not_found`, `member_not_found` |
| 409 | `slot_unavailable`, `booking_limit_reached`, `hold_expired`, `invalid_state`, `booking_started`, `slot_started`, `series_conflict` |
| 422 | `invalid_slot`, `idempotency_key_reuse`, validation errors |
| 500 | `internal_error` (never leaks SQL) |

DB errors are translated in one place: `23P01` → `slot_unavailable`; `23505` on `ledger_entries` should be unreachable (it is caught in tests, a 500 if ever seen).

## 9. Background worker

`python -m courtlock.worker` loops every 30 s:

1. Expire stale holds in batches of 500 (`FOR UPDATE SKIP LOCKED`).
2. Delete idempotency keys older than 24 h.
3. Log counts. Exposes liveness via file touch / log heartbeat.

Idempotent and safe to run on several nodes. Killing it must never change any test outcome (there is a test for exactly that).

## 10. Test strategy

Tests run against **real Postgres** (no SQLite, no mocks for the DB — the guarantees live in Postgres). Each test gets a clean schema via template database / truncation.

| Guarantee | Tests that prove it |
|---|---|
| **G1** no double booking | (a) **N-way race over HTTP:** 100 threads, one slot, barrier-released → exactly 1×201, 99×409, 1 active row. Repeated 50 rounds. (b) **Many slots, many members:** 200 requests over 20 slots → ≤ 1 active row per slot, count of 201s == count of rows. (c) **Direct SQL attack:** two `INSERT`s in separate connections, and overlapping ranges, must raise `ExclusionViolation`; proves the DB, not the app, enforces it. (d) **Hold vs. hold on expiry boundary** with a fake clock. (e) **Hypothesis stateful test:** random sequences of hold/confirm/cancel/expire-by-time-advance compared to an in-memory model; invariant "no overlap among active rows" checked after every step. (f) Different-court / adjacent-hour holds both succeed (no false conflicts). |
| **G1 + BR-6** expiry | Hold, advance clock past TTL, worker **not running**, another member holds the same slot → 201. Confirm on expired hold → 409. Availability shows it free. |
| **G2** retried confirm | Confirm 20× sequentially → same body, 1 ledger CHARGE, 1 CONFIRMED row. Confirm 20× **concurrently** → same. Simulated timeout: server completes, client "never saw" response, retries → same. Cancel replays → 1 REFUND. `Idempotency-Key` hold replays and mismatched-body replay (422). Direct SQL: second ledger row with same `(booking_id, kind)` rejected. |
| **G3** no partial state | Fault injection at every checkpoint in confirm, cancel, hold, recurring create/cancel; assert whole-DB snapshot equality. Recurring where week 3 of 4 conflicts → 409, zero rows (series, bookings, ledger, keys) created. Recurring where the quota would be exceeded at occurrence 3 → nothing written. |
| BR-10 cap | Sequential: 4th hold → 409. **Concurrent:** 10 simultaneous holds on 10 different slots by one member → exactly 3 succeed. Cancel frees one. Expired hold stops counting. |
| BR-11 refund | Unit boundary tests (24h±1s) + HTTP test with fake clock. |
| BR-9 pricing | Unit tests per venue/weekday/window edges; DST day; price locked across a peak boundary between hold and confirm. |
| BR-18 utilization | Seeded fixture with known bookings → exact numbers; cancelled/expired/held excluded; DST week denominator. |
| Contract | Black-box tests for the 4 fixed endpoints exactly as a third party would call them (separate `tests/contract/`, uses only HTTP). |
| Hygiene | `tests/test_no_suppressions.py` greps `src/` and `tests/` for `noqa`/`type: ignore`. |

**Clock:** `clock.py` defines `Clock` (`now() -> datetime`). Production uses the system UTC clock; tests inject a controllable fake through a FastAPI dependency override, which is how we test 5-minute and 24-hour behaviour without sleeping. Every service takes `now` from the clock once, at the start of the use-case.

**Determinism:** concurrency tests use a `threading.Barrier` to release all requests together, and assert on the DB end-state (not on timing).

## 11. Deployment, CI

**docker compose** (`docker compose up`):

| Service | Detail |
|---|---|
| `db` | `postgres:15`, volume, healthcheck `pg_isready` |
| `migrate` | same image as api; `alembic upgrade head` then `python -m courtlock.seed` (idempotent) ; `depends_on: db healthy` |
| `api` | `uvicorn courtlock.main:app`, port 8000, `depends_on: migrate completed_successfully`, `/readyz` healthcheck |
| `worker` | `python -m courtlock.worker` |

Pool: `pool_size=10, max_overflow=10` per api process; Uvicorn threadpool limited to the same ceiling so requests queue in the app, not as DB connection errors.

**CI** (`.github/workflows/ci.yml`, on push + PR):

1. `uv sync --frozen`
2. `ruff check` and `ruff format --check`
3. `mypy --strict src tests`
4. suppression grep
5. `pytest` against a `postgres:15` service container (migrations applied via Alembic, so migrations are tested too; also `alembic check` for model/migration drift)
6. `docker compose up -d --build --wait` + curl the four contract endpoints (smoke) + `docker compose down -v`

## 12. Evolution: real payments (Q-2)

If a payment gateway is added, calling it inside the booking transaction would break G3 (charge succeeds, DB rolls back) and hold DB locks across network I/O. The plan: `payment_intents` rows + **transactional outbox**; confirm moves `HELD → PENDING_PAYMENT → CONFIRMED` via gateway webhook, using the gateway's own idempotency key = booking id. Out of scope for v1 but the one-row state machine and the ledger's uniqueness already fit it.

## 13. Time, scale, and known edges

- App-supplied time (injectable clock) instead of Postgres `now()`: needed for deterministic tests. Cost: with several api instances, clock skew can shift an expiry by the skew. Mitigation option if it matters: compute `now` from `SELECT clock_timestamp()` in production (one extra round trip) and keep the fake clock in tests. Documented in LIMITATIONS.md.
- Hot slot (everybody wants Saturday 10:00): contention is on one index key; losers fail fast with 409 after waiting for the winner's commit (ms).
- Large availability ranges are capped at 14 days; one query per request.
- No rate limiting in v1 (a member can create/release holds in a loop up to the cap; only wasteful, not unsafe).

## 14. Live-change preparation

Changes an examiner might ask for, and where they land:

| Change | Where |
|---|---|
| Hold TTL 5 → 10 min | `HOLD_TTL_SECONDS` env |
| Refund tiers (e.g. 50 % at ≥ 6 h) | `domain/refund.py` only + unit test |
| Cap 3 → 5, or series counts as one | `MAX_ACTIVE_BOOKINGS` / the count query in `domain/quota.py` + `services/` |
| 2-hour bookings | `duration_hours` on request; `CHECK` relaxed; exclusion constraint unchanged |
| Court maintenance blocks | new status `BLOCKED` (or `kind` column) inside the same exclusion constraint |
| Weekend surcharge / holidays | `peak_windows` data or `domain/pricing.py` |
| Different opening hours per weekday | `venue_hours` table; `slots_for` reads it |
| Auth scheme | `api/deps.py` |

## 15. Decision log

| ID | Decision | Reason |
|---|---|---|
| D-01 | Postgres exclusion constraint is the double-booking guard | Enforced for every writer, every instance, no coordination code |
| D-02 | Identity via `X-Member-Id` / `X-Staff-Key` from an upstream gateway | Auth is not in the brief; isolated in one module |
| D-03 | Single `bookings` table; hold id == booking id | No gap between hold release and booking creation; natural confirm idempotency |
| D-04 | Expiry applied by the hold transaction itself; worker is housekeeping | Correctness independent of a job |
| D-05 | `READ COMMITTED` + constraints + row locks | Simple, provable, low contention |
| D-06 | Per-member `SELECT … FOR UPDATE` to serialise the cap | Only way to make a count-then-insert rule race-free without serializable |
| D-07 | Payment = internal ledger with unique `(booking_id, kind)` | Keeps G2/G3 provable; real gateway deferred (§12) |
| D-08 | Price snapshotted at hold time | Member sees what they pay; stable under price/peak changes |
| D-09 | Sync SQLAlchemy + psycopg 3 | DB-bound; simpler typing and transactions |
| D-10 | App-injected clock | Deterministic tests of TTL and the 24 h rule |
| D-11 | Recurring = atomic, one step, N ordinary bookings | Reuses every guarantee for free; no virtual occurrences |
| D-12 | Slots are 1 h, venue-local aligned; DB stores UTC ranges | Matches the brief; range type allows later extension |
| D-13 | Zero inline suppressions, enforced in CI | Stated requirement |

## 16. Alternatives rejected (technical)

| Alternative | Why not |
|---|---|
| **Application-level locks** (Python `threading.Lock`, in-process dict) | Only works with one process; lost on restart. |
| **Redis / distributed lock** around check-then-insert | A second system whose failure (lease expiry, GC pause) re-opens double-booking; the DB is already the single source of truth. |
| **`SELECT … FOR UPDATE` on court row, then check-and-insert** | Correct, but serialises all bookings on a court and relies on every writer remembering to take the lock. The constraint protects writers that forget. |
| **Pre-generated slot rows + `UNIQUE(court_id, start_at)`** | Simple and valid for 1 h slots, but needs a job to materialise slots, breaks for variable-length bookings, and creates a state where slots "don't exist yet". Exclusion constraint on ranges needs no materialisation. |
| **Two tables (`holds`, `bookings`)** | Needs cross-table exclusion (not supported), and a gap at confirm. |
| **`SERIALIZABLE` isolation everywhere** | Valid but converts contention to retry storms and obscures the proof. May be reconsidered as defence in depth. |
| **TTL via Redis key expiry / `pg_cron`-only expiry** | Moves a correctness property to a component that can be down. |
| **Optimistic concurrency (version column) for confirm** | Row lock is simpler and gives a clean "second confirm waits, then replays". |
| **Idempotency-Key only (no natural idempotency) for confirm** | Would force clients to remember a key; natural idempotency works even for naïve retrying clients, which is what the guarantee asks. |
| **Async SQLAlchemy / asyncpg** | See §2. |
| **Calling the payment gateway inside the transaction** | See §12. |
| **Using Postgres `now()` as the only clock** | Hard to test without sleeping or monkeypatching the DB. |

## 17. Security & observability

- Staff key compared with `hmac.compare_digest`; secrets only from env; `.env.example` committed, no real secrets.
- Another member's id returns 404, not 403 (no existence probing).
- Pydantic validation on all inputs; SQL only via SQLAlchemy parameters.
- Logs: JSON, request id (`X-Request-Id` echoed), booking id, outcome code; no PII beyond ids.
- Metrics (stretch): counters for `409 slot_unavailable`, replays, expired holds swept, deadlock retries.
