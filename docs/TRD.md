# CourtLock — Technical Requirements Document (TRD)

| | |
|---|---|
| **Status** | **v0.4** — week 3 implemented: recurring bookings, staff utilization and `GET /courts` on top of the week-2 core. Series cancel, load measurements and the final LIMITATIONS.md are week 4. |
| **Companion** | [DESIGN.md](../DESIGN.md) (PRD: rules `BR-*`, requirements `FR-*`, review response §0) · [README.md](../README.md) (the API contract with curl examples) |

This document says *how* we make three guarantees true:

- **G1** a court is never double-booked, however many requests arrive at once
- **G2** a client retrying a timed-out confirm never ends up with two bookings
- **G3** a failed multi-step operation leaves no partial state

## 1. Principle (restated after review)

> **Invariants are enforced by Postgres. Time comes from Postgres. Policy numbers live in code.**

| Layer | What it owns | Examples |
|---|---|---|
| **Postgres: invariants** | Things that must be true no matter which code, instance or person writes | no overlapping active bookings (exclusion constraint); at most one charge and one refund per booking (unique index); expired holds stop blocking (trigger); all-or-nothing (transactions) |
| **Postgres: the clock** | The single definition of "now" | `clock_timestamp()` — never an application-supplied time (BR-22) |
| **Application code: policy** | Numbers and rules that a client may change | 24 h refund window, 3-booking cap, 5 min TTL, price lookup, 90-day horizon |

The v0.1 wording "the database enforces everything" was too strong: the 24 h threshold and the cap are policy and stay in Python. What must **not** be in Python is anything whose failure produces a double-booking or a duplicate charge, and the **clock** those rules are judged against. v0.1 used an app clock; with two API instances whose clocks differ by 2 s, one instance would evict a hold the other still considers live. Now there is one clock.

## 2. Architecture

```
 client ─▶ gateway (auth, upstream) ─▶ api (FastAPI) ×N  ─▶  Postgres 15  (source of truth + clock)
              X-Member-Id / X-Staff-Key   stateless
```

- **api** — stateless FastAPI processes; any number can run.
- **db** — Postgres 15 with `btree_gist`.
- **migrate** — one-shot compose service: `alembic upgrade head`, then idempotent seed.
- **No worker in v1** (cut). Nothing in the system depends on a background job. See §9.

Layers (dependencies point down only):

```
api/       routers, schemas, error handlers, identity (deps.py — the ONLY place headers are read)
services/  use-cases: one function = one DB transaction
domain/    pure functions, no I/O: slots, pricing, refund, weeks
db/        models, session/unit-of-work, retry helper
```

`domain/` is pure on purpose: refund boundary, DST, and peak windows are unit-tested without a database.

## 3. Stack & tooling

| Concern | Choice |
|---|---|
| Language | Python 3.11 |
| Web | FastAPI + Uvicorn, **sync** endpoints (threadpool) |
| ORM / driver | SQLAlchemy 2.0 (typed `Mapped[]`) + psycopg 3 |
| Migrations | Alembic, hand-reviewed; raw DDL for constraint, trigger, view |
| DB | `postgres:15` |
| Settings | pydantic-settings (env) |
| Tests | pytest + httpx/TestClient against **real Postgres** |
| Lint/format/types | `ruff check`, `ruff format --check`, `mypy --strict` |
| Packaging | `pyproject.toml` + `uv` (lockfile committed) |
| Suppressions | **None** (`# noqa`, `# type: ignore`, …). A test and a CI step grep for them. |

Sync rather than async: the work is DB-bound, the guarantees come from the DB, and sync keeps transactions and mypy typing simple.

## 4. Repository layout (planned)

```
.
├── README.md  DESIGN.md  LIMITATIONS.md (later)  docs/TRD.md
├── docker-compose.yml  Dockerfile  pyproject.toml  uv.lock  alembic.ini
├── alembic/versions/
├── scripts/readme_check.py          # runs the README's curl blocks (CI)
├── src/courtlock/
│   ├── main.py  config.py  seed.py
│   ├── api/       routers/, schemas.py, errors.py, deps.py
│   ├── services/  holds.py, bookings.py, recurring.py, availability.py, utilization.py
│   ├── domain/    slots.py, pricing.py, refund.py, weeks.py
│   └── db/        models.py, session.py, retry.py
├── tests/  unit/  integration/  concurrency/  atomicity/  contract/
└── .github/workflows/ci.yml
```

## 5. Data model

All ids UUID (app-generated). Timestamps `timestamptz` (UTC). Money `integer` minor units.

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE TYPE booking_status AS ENUM ('HELD','CONFIRMED','CANCELLED','EXPIRED');
CREATE TYPE price_band     AS ENUM ('PEAK','OFFPEAK');

venues(id PK, name, timezone text,             -- IANA
       opens_at time, closes_at time,          -- local; CHECK opens_at < closes_at
       peak_price_cents int, offpeak_price_cents int, currency char(3))

peak_windows(id PK, venue_id FK, weekday smallint CHECK 0..6,   -- 0 = Monday, venue-local
             start_time time, end_time time, CHECK start_time < end_time)

courts(id PK, venue_id FK, name, sport, is_active bool DEFAULT true, UNIQUE(venue_id,name))

members(id PK, name, email UNIQUE)

recurring_series(id PK, member_id FK, court_id FK, weeks smallint CHECK 2..12, created_at)

bookings(                                    -- a HOLD is a booking in status HELD; hold id == booking id
  id PK, member_id FK, court_id FK, series_id FK NULL,
  start_at timestamptz, end_at timestamptz,
  during tstzrange GENERATED ALWAYS AS (tstzrange(start_at, end_at, '[)')) STORED,
  status booking_status, band price_band, price_cents int CHECK >= 0, currency char(3),
  hold_expires_at timestamptz NULL,          -- required while HELD
  confirmed_at timestamptz NULL, cancelled_at timestamptz NULL,
  refund_percent smallint NULL CHECK 0..100, refund_cents int NULL,   -- set once, at cancel
  created_at timestamptz,
  CHECK (end_at = start_at + interval '1 hour'),
  CHECK (status <> 'HELD'      OR hold_expires_at IS NOT NULL),
  CHECK (status <> 'CONFIRMED' OR confirmed_at   IS NOT NULL),
  CHECK (status <> 'CANCELLED' OR (cancelled_at IS NOT NULL AND refund_percent IS NOT NULL AND refund_cents IS NOT NULL)),

  -- ★ G1: the double-booking guarantee ★
  EXCLUDE USING gist (court_id WITH =, during WITH &&) WHERE (status IN ('HELD','CONFIRMED')))

ledger_entries(id PK, booking_id FK, member_id FK, kind text CHECK IN ('CHARGE','REFUND'),
               amount_cents int CHECK > 0, currency, created_at,
               UNIQUE(booking_id, kind))     -- ★ G2: at most one charge and one refund per booking ★
```

### 5.1 Expiry trigger — makes BR-6 true for every writer

The exclusion constraint cannot reference `now()` (index predicates must be immutable), so a `HELD` row that has timed out would still block. Instead of teaching each code path to evict stale holds (our v0.1 mistake), the **table** does it:

```sql
CREATE FUNCTION evict_expired_holds() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IN ('HELD','CONFIRMED') THEN
    UPDATE bookings
       SET status = 'EXPIRED'
     WHERE court_id = NEW.court_id
       AND during && tstzrange(NEW.start_at, NEW.end_at, '[)')   -- NEW.during is not computed yet in a BEFORE trigger
       AND status = 'HELD'
       AND hold_expires_at <= clock_timestamp();
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER bookings_evict_expired
  BEFORE INSERT ON bookings FOR EACH ROW EXECUTE FUNCTION evict_expired_holds();
```

A `BEFORE INSERT` trigger runs *before* the exclusion constraint is checked, in the same transaction, so a freed slot is insertable. Any insert — hold, recurring, a future maintenance block, a hand-typed `psql` statement — gets this behaviour; no code path can forget it.

### 5.2 The single definition of "live" — BR-23

```sql
CREATE VIEW live_bookings AS
SELECT * FROM bookings
 WHERE status = 'CONFIRMED'
    OR (status = 'HELD' AND hold_expires_at > clock_timestamp());
```

Availability, the member cap, the hold-replay lookup and the series pre-check — every read that asks "what blocks a slot / what counts toward the cap" — go through `live_bookings`, so there is no second definition to get wrong. (State transitions — confirm, cancel — necessarily address one row by id and spell out their own `status`/`hold_expires_at` conditions in the `UPDATE`; those are covered by the G2 tests.)

### 5.3 Indexes

```sql
CREATE INDEX bookings_member_future_idx ON bookings (member_id, start_at) WHERE status IN ('HELD','CONFIRMED');
CREATE INDEX bookings_court_time_idx    ON bookings (court_id, start_at);
```

### 5.4 Why this shape

- **One `bookings` table for holds and bookings.** Confirm is a state change on one row; there is no window between "hold released" and "booking created". The hold id *is* the booking id.
- **Slot alignment is app-validated**, not SQL: it depends on the venue zone (incl. half-hour offsets such as IST).
- **Zero-price bookings** write no ledger row (`amount_cents > 0`).

## 6. Concurrency & correctness design

### 6.1 G1 — never double-booked

Every active row is created by an `INSERT` into `bookings` (hold, or recurring). All inserts pass the exclusion constraint.

- Two transactions inserting overlapping ranges for one court: the second **blocks** on the first's uncommitted row; if the first commits, the second fails with SQLSTATE `23P01` → we return `409 slot_unavailable`; if the first rolls back, the second succeeds. True at `READ COMMITTED`.
- The application does not rely on check-then-insert (a cheap pre-check may give nicer errors; the constraint is the authority).
- Code that bypasses the API cannot violate G1.

### 6.2 Holds never block forever — and the recurring walk-through

**The scenario the review asked about.** Member A holds Saturday 10:00 on court 1 at week 2 of a series that Member B now wants to book weekly. A's hold timed out 10 minutes ago. The sweeper does not exist; the row is still `status='HELD'`.

*v0.1 trace (broken):*
1. `POST /recurring-bookings` begins; inserts occurrence 1 (fine), occurrence 2.
2. Occurrence 2's INSERT collides with A's stale `HELD` row → `23P01` → `409 series_conflict`.
3. B is told a free slot is taken; A's dead hold blocks B **forever** (no sweeper). BR-6 is violated. The eviction `UPDATE` existed only in `create_hold()`.

*v0.2 trace (correct):*
1. `POST /recurring-bookings` begins one transaction; locks B's `members` row; reads `now = clock_timestamp()`.
2. **Pre-check** reads `live_bookings` for the N target slots. A's row is not live (`hold_expires_at <= clock_timestamp()`), so it is **not** reported as a conflict.
3. Inserts occurrences in ascending order. Before each insert the `BEFORE INSERT` trigger runs and sets A's stale row to `EXPIRED`. The exclusion constraint is then checked and passes.
4. N ledger charges; `COMMIT`. B has the full series; A's row is `EXPIRED`.

*Variants:*

| Situation | Outcome |
|---|---|
| Stale hold on week 2, **live** hold on week 4 | Pre-check reports exactly week 4 → `409 series_conflict`, `conflicts:[week 4]`. Rollback: nothing written; A's stale hold is untouched (still `HELD`, still expired). |
| Stale hold raced by a concurrent single hold | Both transactions try to evict the same stale row; one waits on the row lock, re-reads after the other commits. Exactly one insert wins the constraint; the other gets `409`. |
| Raw `psql` `INSERT` over a stale hold | Succeeds (trigger). Over a live hold: `23P01`. |
| A confirms the hold at the last instant | Confirm's conditional `UPDATE` (§6.3) takes the row lock first; the trigger's eviction then re-evaluates `status='HELD'` after the lock is released, finds `CONFIRMED`, evicts nothing; the INSERT fails the constraint → `409`. |

### 6.3 G2 — retries

**Confirm** is a single conditional `UPDATE` plus a classification read; no application-level decision between them:

```sql
BEGIN;
UPDATE bookings
   SET status = 'CONFIRMED', confirmed_at = clock_timestamp()
 WHERE id = :id AND member_id = :me
   AND status = 'HELD' AND hold_expires_at > clock_timestamp()
RETURNING *;
-- 1 row  → INSERT ledger_entries(CHARGE, price_cents) [if price > 0]; return 200
-- 0 rows → SELECT status, hold_expires_at … WHERE id=:id AND member_id=:me :
--            no row                  → 404 hold_not_found           (also for someone else's hold)
--            CONFIRMED               → 200, same body, header Idempotent-Replay: true   (the retry case)
--            HELD past expiry / EXPIRED → 409 hold_expired
--            CANCELLED               → 409 invalid_state
COMMIT;
```

A retry that arrives while the first attempt is still running blocks on the row lock inside `UPDATE`, then (READ COMMITTED re-evaluates `WHERE`) matches 0 rows, sees `CONFIRMED`, and replays. No second row exists; `UNIQUE(booking_id,'CHARGE')` is the structural backstop. The expired branch **writes nothing**, so there is no error path that needs to commit.

**Cancel** locks the row, then decides:

```sql
BEGIN;
SELECT * FROM bookings WHERE id=:id AND member_id=:me FOR UPDATE;
now := clock_timestamp()                                  -- read after the lock
  none                          → 404 booking_not_found
  CANCELLED                     → 200, stored cancelled_at / refund (replay; no new ledger row)
  HELD live (hold_expires_at>now) → release: status=CANCELLED, refund_percent=0, refund_cents=0
  HELD past expiry / EXPIRED    → 409 hold_expired
  CONFIRMED, start_at <= now    → 409 booking_started
  CONFIRMED                     → percent = refund_percent(start_at, now)   -- 100 if start_at-now >= 24h else 0
                                  UPDATE … status=CANCELLED, cancelled_at=now, refund_percent, refund_cents
                                  INSERT ledger REFUND [if refund_cents > 0]
COMMIT;
```

**Hold retry** (`POST /holds` whose response was lost): after locking the member row, we look in `live_bookings` for *this member's* `HELD` row for the same court and start. If found, return it with `200` (new holds are `201`). No `Idempotency-Key` table is needed (cut).

### 6.4 G3 — no partial state

- **One use-case = one transaction.** Nothing commits until the function returns; any exception rolls back everything (booking rows, ledger rows, series row).
- **No external side effects inside a transaction.** v1 has none (ledger rows, not a gateway; no email/HTTP). If added, use an outbox (§17).

| Operation | Atomic unit |
|---|---|
| create hold | member lock, replay check, cap check, insert (+ trigger eviction) |
| confirm | status → CONFIRMED, charge row |
| cancel | status → CANCELLED + refund fields, refund row |
| recurring create | series row, N booking rows (+ evictions), N charge rows |
| series cancel (should) | N cancellations, ≤ N refund rows |

**Proof — failure injection with no hooks in production code.** Tests install a *temporary DB trigger* that raises on a chosen statement (e.g. "fail on the 2nd INSERT into `ledger_entries`", "fail the UPDATE of `bookings` to CANCELLED", "fail on the 3rd INSERT into `bookings`"), call the endpoint, expect `500`, and assert a snapshot of every table equals the snapshot before. Also: a test terminates the backend (`pg_terminate_backend`) mid-transaction. Production code has no `if TESTING` branches or fault points.

### 6.5 Cap under concurrency; lock order; deadlocks

Two simultaneous holds by one member at count 2 would both see "2 < 3". Serialise per member:

```sql
SELECT id FROM members WHERE id = :me FOR UPDATE;       -- first statement of hold & recurring
SELECT count(DISTINCT COALESCE(series_id, id))          -- series counts once (BR-10)
  FROM live_bookings
 WHERE member_id = :me AND start_at > :now;
```

Different members never contend. Confirm and cancel need no cap logic.

**Lock order:** `members` row → booking inserts in ascending `(court_id, start_at)`. Two recurring requests from different members over interleaved slots can still deadlock; Postgres aborts one (`40P01`). `db/retry.py` re-runs the whole use-case (safe: one transaction) up to 3 times with jitter on `40P01`/`40001`.

### 6.6 Isolation

`READ COMMITTED` + constraints + targeted locks. `SERIALIZABLE` rejected as the main mechanism (§16): correct but turns contention into retry storms and obscures the proof.

## 7. API contract

Conventions: JSON, `snake_case`, ISO-8601 timestamps (responses in UTC `Z`), no version prefix (paths are fixed). Errors are `application/problem+json` (§8). **README.md is the user-facing contract with copy-pasteable curl for every call; this section is the same contract in design form.**

**Identity (D-02) — one place.** `api/deps.py` exposes `current_member()` (reads `X-Member-Id`, returns a `MemberId`) and `require_staff()` (reads `X-Staff-Key`, `hmac.compare_digest` against `STAFF_API_KEY`). Routers and services receive typed values and never see headers. Replacing this with JWT/OIDC edits only `deps.py`. A test asserts the strings `X-Member-Id` and `X-Staff-Key` appear in no other source file.

### 7.1 `POST /holds` — member

Body: `{"court_id": "<uuid>", "start_at": "<ISO-8601 with offset>"}`

`201` new hold, **`200` same member repeating a live hold for the same slot**:

```json
{"id":"…","court_id":"…","member_id":"…",
 "start_at":"2026-12-01T10:00:00Z","end_at":"2026-12-01T11:00:00Z",
 "status":"HELD","price_band":"OFFPEAK","price_cents":1200,"currency":"USD",
 "expires_at":"2026-11-30T09:05:00Z"}
```

Errors: `401 missing_identity`, `404 member_not_found | court_not_found`, `409 slot_unavailable | booking_limit_reached`, `422 invalid_slot | validation_error`.

Flow: lock member → `now` → validate slot (aligned, inside hours, future, ≤ horizon) → replay lookup → cap → price → `INSERT` (trigger evicts stale; constraint decides). `hold_expires_at = least(now + TTL, start_at)`.

### 7.2 `POST /holds/{id}/confirm` — member

No body. `200` first call and every replay (replay adds `Idempotent-Replay: true`):

```json
{"id":"…","court_id":"…","member_id":"…","status":"CONFIRMED","confirmed_at":"…",
 "start_at":"…","end_at":"…","price_band":"OFFPEAK","price_cents":1200,"currency":"USD"}
```

Errors: `404 hold_not_found`, `409 hold_expired | invalid_state`.

### 7.3 `POST /bookings/{id}/cancel` — member

No body. `200`:

```json
{"id":"…","status":"CANCELLED","cancelled_at":"…",
 "price_cents":1200,"refund_percent":100,"refund_cents":1200,"currency":"USD"}
```

Replay returns the **stored** values. Cancelling a live hold: `refund_percent 0`, `refund_cents 0`. Errors: `404 booking_not_found`, `409 booking_started | hold_expired`.

### 7.4 `GET /courts/{id}/availability?from=YYYY-MM-DD&to=YYYY-MM-DD` — no auth

`from`/`to` are **venue-local dates**; `to` exclusive (default `from + 1 day`); `from` default = today (venue-local); max span 14 days.

```json
{"court_id":"…","timezone":"UTC",
 "slots":[{"start_at":"2026-12-01T06:00:00Z","end_at":"2026-12-01T07:00:00Z",
           "status":"AVAILABLE","price_band":"OFFPEAK","price_cents":1200,"currency":"USD"}, …]}
```

`status` ∈ `AVAILABLE | HELD | BOOKED`. Started slots omitted. Reads `live_bookings`, so expired holds appear `AVAILABLE`. No member identity exposed. Errors: `404 court_not_found`, `422 validation_error`.

### 7.5 `POST /recurring-bookings` — member (week 3)

Body: `{"court_id":"…","first_start_at":"2026-12-01T10:00:00Z","weeks":12}`

`201`:

```json
{"series_id":"…","court_id":"…","member_id":"…","weeks":12,
 "total_price_cents":14400,"currency":"USD",
 "bookings":[{"id":"…","start_at":"2026-12-01T10:00:00Z","end_at":"…","status":"CONFIRMED",
              "price_band":"OFFPEAK","price_cents":1200}, …]}
```

Errors: `409 series_conflict` with `"conflicts":[{"start_at":"…"}]` (from a pre-check that ignores expired holds; a request that loses a race for a week gets the same code without a list; nothing is booked either way); `409 booking_limit_reached`; `422 invalid_slot` (any occurrence outside hours, not aligned, on a local time that does not exist, or the first slot past/beyond the horizon) and `422 validation_error` (`weeks` outside 2–12 or a malformed body). `404 member_not_found | court_not_found`. Occurrences: same **local** wall-clock time every 7 days in the venue zone. Atomic (§6.4).

### 7.6 `GET /staff/utilization?week=2026-W48[&venue_id=…]` — staff (week 3)

```json
{"week":"2026-W48","week_start":"2026-11-23","week_end":"2026-11-30",
 "venues":[{"venue_id":"…","name":"Riverside Sports Club","timezone":"UTC",
            "booked_hours":31,"bookable_hours":224,"utilization_pct":13.8,
            "courts":[{"court_id":"…","name":"Court 1","booked_hours":20,"bookable_hours":112,
                       "utilization_pct":17.9,"peak_hours":9,"offpeak_hours":11}]}]}
```

`week_end` exclusive. Errors: `401 invalid_staff_key`, `422 validation_error`.

### 7.7 `GET /courts` (no auth) and `GET /healthz`

`GET /courts` returns `{"courts":[{id, name, sport, venue_id, venue_name, timezone}]}` for active courts, ordered by venue then court name. `POST /recurring-bookings/{series_id}/cancel` (cancel a whole series atomically) is still *should*, planned for week 4.

## 8. Error model

`application/problem+json`: `{"type":"about:blank","title":"…","status":409,"code":"slot_unavailable","detail":"…"}` — **switch on `code`**.

| Status | Codes |
|---|---|
| 401 | `missing_identity`, `invalid_staff_key` |
| 404 | `member_not_found`, `court_not_found`, `venue_not_found`, `hold_not_found`, `booking_not_found` |
| 409 | `slot_unavailable`, `booking_limit_reached`, `hold_expired`, `invalid_state`, `booking_started`, `series_conflict` |
| 422 | `invalid_slot`, `validation_error` |
| 500 | `internal_error` (never leaks SQL) |

`23P01` → `slot_unavailable` (or `series_conflict` for recurring). `23505` on `ledger_entries` is unreachable by design; if seen it is a 500 and a bug.

## 9. No background worker (cut) — what that means

Nothing needs one. Expired holds are ignored by the trigger, the view, and the conditional `UPDATE`s. Rows stay `HELD` past expiry. Optional hygiene, not shipped:

```sql
UPDATE bookings SET status='EXPIRED' WHERE status='HELD' AND hold_expires_at <= clock_timestamp();
```

(Documented for operators; safe to run any time; covered by a test that runs it and checks no behaviour changes.)

## 10. Domain logic (pure, `domain/`)

- **Slots:** `slots_for(venue, local_date)` builds zone-aware local datetimes `opens_at … closes_at` in 1-hour steps → UTC. DST: a non-existent local hour is skipped; an ambiguous one uses `fold=0`. `validate_slot(venue, start_at, now)` rejects: not on a venue-local hour boundary, outside opening hours, `start_at ≤ now`, `start_at > now + horizon`.
- **Pricing:** `price_for(venue, start_at)` → `(band, cents)`; peak iff the venue-local weekday has a window with `start_time ≤ local_time < end_time`. Snapshotted onto the row at hold time.
- **Refund:** 

```python
def refund_percent(start_at: datetime, now: datetime) -> int:
    return 100 if start_at - now >= timedelta(hours=24) else 0
```

  `now` is the DB's `clock_timestamp()` fetched inside the transaction after the lock. Unit tests: 24 h + 1 s, exactly 24 h, 24 h − 1 s.
- **Recurring occurrences:** `occurrences(venue, first_start_at, weeks)` → local wall-clock +7 days each, converted to UTC, each validated as a slot.
- **Utilization (week 3):** week = ISO week, Mon 00:00 → next Mon 00:00 venue-local. `bookable_hours` = slots from `slots_for` over the 7 local dates (DST-correct), active courts only. `booked_hours` = `SELECT court_id, band, count(*) FROM bookings WHERE status='CONFIRMED' AND start_at >= :ws AND start_at < :we GROUP BY …`.

## 11. Test strategy

**Status (week 3):** 203 tests, all passing (unit 54, integration 108, concurrency 28, atomicity 6, contract 3, hygiene 4; see `tests/`). Built and passing: every G1, G2 and G3 row below for hold / confirm / cancel **and for recurring bookings**; T-R1 to T-R5 (the expired-but-unswept-hold walk-through, in `integration/test_recurring.py`, `concurrency/test_double_booking.py` and `concurrency/test_recurring_races.py`); BR-6, BR-9, BR-10 (series counted once), BR-11, BR-18 (utilization, including clock-change weeks and venue-local week boundaries); the README walkthrough and the hygiene checks. Still **not built** (week 4): the Hypothesis stateful test (*should*), series cancel, and a load measurement against NFR-1.

Guarantee tests were checked by sabotage: removing the exclusion constraint fails 14 G1 tests; removing the expiry trigger fails the 3 BR-6 tests; removing the per-member lock fails the cap race test. For recurring: recreating the v0.1 bug (a series pre-check that reads raw `HELD` rows instead of `live_bookings`) fails T-R1 and T-R2; dropping the expiry trigger fails T-R1; counting every occurrence toward the cap (the v0.1 reading) fails the two series-cap tests.

Real Postgres everywhere (no SQLite, no DB mocks): the guarantees live in Postgres.

**Time without a fake clock** (consequence of BR-22):
- *Expiry:* tests **back-date** `hold_expires_at` with SQL (`UPDATE bookings SET hold_expires_at = clock_timestamp() - interval '1 second' WHERE id = …`). One slow end-to-end test uses `HOLD_TTL_SECONDS=1` and sleeps 1.2 s.
- *24 h rule:* the exact boundary is a **unit test** on `refund_percent`; HTTP/integration tests use slots 25 h and 23 h away (±1 h margin).
- *Price lock:* change the venue price between hold and confirm; the charge equals the snapshot.

| Guarantee | Tests |
|---|---|
| **G1** | **(a)** 100 threads, one slot, released by a `Barrier` → exactly 1×201, 99×409, 1 active row; repeated 50 rounds. **(b)** 200 requests over 20 slots from many members → ≤ 1 active row per slot; #201 == #rows. **(c)** Direct SQL: overlapping inserts in two connections raise `ExclusionViolation`. **(d)** Adjacent hours and different courts don't conflict. **(e)** *(should)* Hypothesis stateful test vs an in-memory model; invariant "no overlap among active rows" after every step. |
| **BR-6** expiry | Hold, back-date expiry, **no cleanup**, another member holds the slot → 201. Confirm after expiry → 409. Availability shows it free. |
| **Recurring × expired hold** *(the review's case)* | **T-R1** B books a 4-week series; A's hold on week 2 is back-dated expired and unswept → `201`, A's row now `EXPIRED`. **T-R2** Same, plus a *live* hold on week 4 → `409 series_conflict` listing exactly week 4; **every table identical to before** (stale hold still `HELD`). **T-R3** Raw-SQL `INSERT` over an expired hold succeeds; over a live hold fails. **T-R4** Single hold over an expired hold succeeds (the v0.1 path). **T-R5** 20 concurrent recurring requests for one court/time → exactly 1 succeeds, none leaves a partial series; concurrent recurring + single hold on overlapping slots → exactly one wins; no `500` from deadlocks. |
| **G2** | Confirm ×20 sequential → same body, 1 CHARGE, 1 CONFIRMED row. Confirm ×20 **concurrent** → same. "Lost response" simulation (server completes, client discards, retries) → same. Cancel ×20 → 1 REFUND. Repeat `POST /holds` → same hold. Direct SQL: second ledger row with same `(booking_id, kind)` rejected. |
| **G3** | Failure injection by temporary DB triggers (§6.4) for every statement of hold, confirm, cancel, recurring (week 3) → whole-DB snapshot identical. Recurring where occurrence 3 of 4 conflicts, and where the cap would be exceeded → nothing written. `pg_terminate_backend` mid-transaction → nothing written. |
| **BR-10** cap | Sequential: 4th → 409. **Concurrent:** 10 simultaneous holds, one member, 10 slots → exactly 3 succeed. Cancel frees one. Expired hold stops counting. **Series:** 12-week series + 2 singles = 3 OK; 4th → 409; cancelling one occurrence does not free cap; cancelling the last does. |
| BR-11 | Unit boundary tests + HTTP at 25 h / 23 h. |
| BR-9 | Unit: venue/weekday/window edges, DST day; price locked across venue price change. |
| BR-18 | Seeded fixture → exact numbers; cancelled/held excluded; DST week denominator. |
| Contract | `tests/contract/` uses only HTTP, with the exact bodies and headers shown in the README. |
| README | `scripts/readme_check.py` extracts the README's `bash` blocks and runs them in order against the compose stack (CI); fails on any non-2xx other than ones the README marks as expected. |
| Hygiene | No `noqa` / `type: ignore`; header names only in `deps.py`; services never import FastAPI; `domain/` imports no framework or database code. |

## 12. Deployment & CI

**docker compose up**

| Service | Detail |
|---|---|
| `db` | `postgres:15`, volume, `pg_isready` healthcheck |
| `migrate` | api image; `alembic upgrade head` then `python -m courtlock.seed` (idempotent; fixed UUIDs); `depends_on: db healthy` |
| `api` | `uvicorn --factory courtlock.main:create_app` on :8000; `depends_on: migrate completed_successfully`; `/healthz` healthcheck; env `DATABASE_URL`, `STAFF_API_KEY`, `HOLD_TTL_SECONDS`, `MAX_ACTIVE_BOOKINGS`, `BOOKING_HORIZON_DAYS` |

Pool: `pool_size=10, max_overflow=10` per process; Uvicorn threadpool bounded to the same ceiling.

**CI** (`.github/workflows/ci.yml`): `uv sync --frozen` → `ruff check` + `ruff format --check` → `mypy` (strict, configured in `pyproject.toml`; covers `src`, `tests`, `scripts`) → suppression grep → `pytest` against a `postgres:15` service (the tests build their database by running the real Alembic migration) → `docker compose up -d --build --wait` → `scripts/readme_check.py` → `docker compose down -v`. Week 2 ships all of these except the recurring/utilization portions.

## 13. Time, scale, known edges

- **One clock.** `clock_timestamp()` is read inside the transaction *after* the first lock (so a request that waited on a lock evaluates expiry at the moment it acts, not at transaction start; `now()` would be the transaction start and could be stale). If the DB clock steps (bad NTP), expiry and the 24 h rule move with it. Single primary assumed; with replicas, writes still go to the primary.
- Hot slot: contention is one index key; losers fail fast with 409.
- Availability span capped at 14 days.
- No rate limiting (a member can create/release holds in a loop; wasteful, not unsafe).

## 14. Live-change preparation

| Change | Where |
|---|---|
| Hold TTL | `HOLD_TTL_SECONDS` |
| Refund tiers (e.g. 50 % ≥ 6 h) | `domain/refund.py` + unit test |
| Cap value; series counts per occurrence instead | `MAX_ACTIVE_BOOKINGS`; the `COUNT(DISTINCT COALESCE(series_id,id))` query (drop the `COALESCE`) |
| 2-hour bookings | `duration_hours` in request; relax `CHECK`; exclusion constraint unchanged |
| Court maintenance blocks | new `kind`/status inside the same constraint (trigger and view already apply) |
| Holidays / weekend pricing | `peak_windows` data or `domain/pricing.py` |
| Per-weekday hours | `venue_hours` table; `slots_for` reads it |
| Auth scheme | `api/deps.py` only |

## 15. Decision log

| ID | Decision | Reason |
|---|---|---|
| D-01 | Exclusion constraint is the double-booking guard | Enforced for every writer and instance |
| D-02 | Identity via `X-Member-Id` / `X-Staff-Key`, read only in `api/deps.py` | Auth not in the brief; cheap to change |
| D-03 | Single `bookings` table; hold id == booking id | No gap at confirm; natural confirm idempotency |
| D-04 | **`BEFORE INSERT` trigger evicts expired holds** *(replaces v0.1 app-side eviction)* | Every insert path gets BR-6; fixes the recurring bug |
| D-05 | `READ COMMITTED` + constraints + row locks | Simple and provable |
| D-06 | Per-member `SELECT … FOR UPDATE` serialises the cap | Race-free count-then-insert |
| D-07 | Payment = internal ledger, `UNIQUE(booking_id, kind)` | Keeps G2/G3 provable |
| D-08 | Price snapshotted at hold | Stable under price changes |
| D-09 | Sync SQLAlchemy + psycopg 3 | DB-bound; simpler |
| D-10 | **DB clock (`clock_timestamp()`), no app clock** *(replaces v0.1 injectable clock)* | One time source across instances |
| D-11 | Recurring = atomic, one step, N ordinary bookings | Reuses every guarantee |
| D-12 | 1 h slots, venue-local aligned, UTC ranges | Brief; range type extends later |
| D-13 | Zero inline suppressions, enforced in CI | Stated requirement |
| D-14 | **`live_bookings` view = the one definition of "live"** | No second definition to get wrong |
| D-15 | **Series counts once toward cap** *(replaces per-occurrence)* | "Weekly recurring" must be usable; cost in DESIGN §14 |
| D-16 | `POST /holds` replay returns member's live hold; no idempotency-key table | Covers real retries with less machinery |
| D-17 | Failure injection by temporary DB triggers, not production hooks | No test-only code in production paths |
| D-18 | No worker in v1 | Nothing depends on it |

## 16. Alternatives rejected (technical)

| Alternative | Why not |
|---|---|
| App-level locks / in-memory state | Single-process only; lost on restart. |
| Redis / distributed lock | A second system whose lease expiry reopens double-booking. |
| `SELECT … FOR UPDATE` on court row then check-and-insert | Correct but serialises a court and relies on every writer remembering the lock. |
| Pre-generated slot rows + `UNIQUE(court_id, start_at)` | Needs a materialising job; breaks for variable lengths. |
| Two tables (`holds`, `bookings`) | No cross-table exclusion; gap at confirm. |
| `SERIALIZABLE` everywhere | Retry storms; muddies the proof. |
| **Eviction helper called from each service** | The v0.1 bug: a new path forgets it. A trigger cannot be forgotten. |
| **Eviction by a worker / cron / Redis TTL** | Moves a correctness property to a component that can be down. |
| **Application clock / injectable fake clock** | Skew between instances; contradicts the principle. |
| Postgres `now()` | Transaction-start time can be stale after a lock wait; `clock_timestamp()` is read when we act. |
| Idempotency-Key table for everything | Natural idempotency covers confirm/cancel; hold replay covers holds. |
| Production fault-injection hooks | Test-only code in the shipped path. |
| Async SQLAlchemy | See §3. |
| Gateway call inside the transaction | Breaks G3 (charge succeeds, DB rolls back). See §17. |

## 17. Evolution: real payments (Q-2)

Calling a gateway inside the booking transaction would break G3 and hold locks across network I/O. Plan if needed: `payment_intents` + transactional outbox; confirm becomes `HELD → PENDING_PAYMENT → CONFIRMED` driven by a webhook; gateway idempotency key = booking id. The one-row state machine and ledger uniqueness already fit that.

## 18. Security & observability

- Staff key via `hmac.compare_digest`; secrets from env only; `.env.example` committed, no real secrets. The seed staff key is dev-only and documented as such.
- Another member's id returns 404, not 403.
- Pydantic validation on all inputs; SQL only through SQLAlchemy parameters (raw DDL only in migrations).
- Plain structured-ish logging with outcome codes; no PII beyond ids. (JSON logs / metrics cut.)
