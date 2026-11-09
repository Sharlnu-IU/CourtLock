# CourtLock

**Court booking backend that cannot double-book.**

A community club rents sports courts by the hour across a few venues. CourtLock lets members browse availability, hold a court briefly, confirm it, cancel it, and book it weekly — while the database guarantees that no court is ever booked twice, retries never create duplicates, and a failed operation never leaves half-written state.

> **Status: design phase (week 1).** The documents below are written; **no application code exists yet**. Sections marked 🚧 describe the intended behaviour and will be made true (and verified) as the build proceeds. Please review the design before we build.

## Documents

| Doc | Purpose |
|---|---|
| [DESIGN.md](DESIGN.md) | Product requirements (PRD): goals, business rules, assumptions, gaps in the brief and how we resolved them, **open questions for the client**, alternatives rejected |
| [docs/TRD.md](docs/TRD.md) | Technical design: data model, API shapes, how each guarantee is enforced and tested, CI, decision log |
| `LIMITATIONS.md` | How the system can still fail — *written at the end of the build* |

## The three guarantees

| # | Guarantee | Enforced by |
|---|---|---|
| G1 | A court is never double-booked, however many requests arrive at once | Postgres **exclusion constraint** on `(court_id, time range)` for active rows |
| G2 | Retrying a timed-out confirm never produces two bookings (or two charges) | One-row state machine + `SELECT … FOR UPDATE` + `UNIQUE (booking_id, kind)` on the ledger |
| G3 | A failed multi-step operation leaves no partial state | One use-case = one DB transaction; no external side effects inside it; proven by fault injection |

Every guarantee has automated tests that would fail if it were broken (see TRD §10).

## How booking works

```
 browse            reserve (≤ 5 min)             confirm                 cancel
GET /courts/{id}/  POST /holds ───────────────▶  POST /holds/{id}/  ───▶ POST /bookings/{id}/
availability       slot is blocked for you       confirm                 cancel
                   hold auto-expires             price was locked        ≥ 24 h before start → full refund
                   (never blocks forever)        safe to retry           < 24 h → no refund
```

Rules in one glance (full list: DESIGN.md §5):

- 1-hour slots on the hour, venue-local time; bookable up to 90 days ahead
- Peak/off-peak price per venue; **price is locked when you hold**
- At most **3 active future bookings** per member (unexpired holds count)
- Weekly recurring booking is **all-or-nothing**
- Staff get weekly **utilization** per court and venue

## API (fixed contract)

| Method & path | What it does |
|---|---|
| `POST /holds` | Reserve a slot briefly. Body: `{"court_id", "start_at"}` |
| `POST /holds/{id}/confirm` | Turn the hold into a booking. Safe to retry |
| `POST /bookings/{id}/cancel` | Cancel; refund by the 24 h rule |
| `GET /courts/{id}/availability` | Slots with status `AVAILABLE` / `HELD` / `BOOKED` and price |

Plus (our design): `POST /recurring-bookings`, `GET /staff/utilization`, `GET /venues`, `GET /me/bookings`, `DELETE /holds/{id}`. Exact request/response shapes: [TRD §5](docs/TRD.md).

Identity is supplied by an upstream gateway: `X-Member-Id` (members), `X-Staff-Key` (staff). Login is out of scope.

## Stack

Python 3.11 · FastAPI · SQLAlchemy 2 + Alembic · PostgreSQL 15 · pytest · ruff + mypy (strict, **no inline suppressions**) · Docker Compose · GitHub Actions

## Running it 🚧

*(Planned — not yet implemented.)*

```bash
docker compose up --build
```

This will start Postgres, run migrations and seed demo data (venues, courts, members), then serve the API on <http://localhost:8000> (interactive docs at `/docs`) with the expiry worker alongside.

## Development 🚧

*(Planned.)*

```bash
uv sync                          # install deps
docker compose up -d db          # Postgres for tests
uv run alembic upgrade head
uv run pytest                    # unit + integration + concurrency + atomicity + contract
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src tests
```

CI (`.github/workflows/ci.yml`) runs the same checks plus a compose smoke test, and fails on any `# noqa` / `# type: ignore`.

## Project layout (planned)

```
src/courtlock/
  api/       HTTP layer: routers, schemas, errors, identity
  services/  use-cases, one DB transaction each
  domain/    pure rules: slots, pricing, refunds, quota, weeks
  db/        models, unit of work, deadlock retry
  worker.py  expiry sweeper (housekeeping only)
tests/       unit · integration · concurrency · atomicity · contract
```

## Open questions

The brief is intentionally incomplete; we made and documented a decision for each gap. The ones most worth a client conversation are in [DESIGN.md §10](DESIGN.md) — especially **how recurring bookings count toward the 3-booking cap (Q-1)** and **what request shapes the contract tests expect (Q-9)**.
