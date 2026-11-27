# CourtLock — Limitations

**Status: draft (week 3).** Finalized in week 4 after the load check and the series-cancel work.

This is how the system can still fail or surprise you. Each item says what happens, how likely or severe it is, and what would fix it. Items marked **by design** are decisions we made on purpose (see DESIGN.md); the rest are gaps.

What the system *does* guarantee, and how that was checked, is in the README and TRD §11: no double-booking, no duplicate booking or charge on a retried confirm or cancel, and no partial state after a failed multi-step operation. Everything below is outside those three guarantees, or a condition under which they would not hold.

## 1. Where the guarantees stop

| # | Limitation | What happens | Severity | Fix / mitigation |
|---|---|---|---|---|
| 1 | **The guarantees rely on one Postgres primary and its clock.** | If the database is down, every booking call fails (500). If the database clock is stepped (bad NTP), hold expiry and the 24-hour refund rule move with it. Replicas are not supported for writes. | High impact, low likelihood | Run Postgres with managed failover and NTP monitoring; never point two primaries at the app. |
| 2 | **Concurrency was proven at ~100 simultaneous requests, on two API instances in one test process on one machine.** | We have not tested separate hosts, network partitions, or sustained load. The guarantees come from Postgres, so we expect them to hold, but that is reasoning, not measurement. | Medium | Week 4: a load run against the compose stack. Test with real network latency between API and database. |
| 3 | **Deadlocks are retried only 3 times.** | Two racing recurring requests that interleave unluckily could exhaust the retries and return `500 internal_error`. Nothing is half-written (the transaction rolled back); the client can simply retry. Never seen in the 40-way random race test, because rows are always locked in ascending order. | Low | Raise the retry count, or add jitter. |
| 4 | **Connection pool exhaustion.** | Each API process has 10 + 10 database connections. Under heavy load requests wait up to 30 s for a connection, then fail with 500. The booking rules are unaffected. | Medium under load | Size the pool to the load; add a gateway with rate limiting; measure in week 4. |
| 5 | **A failed transaction is not the same as a crash of the whole database host.** | We tested a killed connection mid-transaction. We did not test power loss; this relies on Postgres durability (`fsync`) being left on. | Low | Keep Postgres defaults. |

## 2. Things that are not real yet

| # | Limitation | What happens | Fix |
|---|---|---|---|
| 6 | **Identity is not authentication.** The API trusts the `X-Member-Id` header, and `X-Staff-Key` is one shared static secret. | Anyone who can reach the API directly can act as any member, or read utilization if they know the key. This is safe only behind a gateway that authenticates users and strips these headers from outside traffic. **By design for v1**; isolated in `api/deps.py` so the fix is one file. | Replace `deps.py` with JWT/OIDC; per-user staff accounts and key rotation. |
| 7 | **Payment is a ledger, not money.** "Charge" and "refund" are rows. | Nothing is collected or returned. A real gateway cannot be called inside the booking transaction without breaking "no partial state"; the plan is an outbox (TRD §17). | Real gateway integration. |
| 8 | **The seed data and `dev-staff-key` are demo values.** | They are public in this repository. | Never use them outside a demo; set `STAFF_API_KEY` and remove the seed in production. |
| 9 | **No rate limiting.** | A member can create and release holds in a loop up to the cap, and the staff key can be brute-forced. Wasteful, not unsafe for bookings. | Rate limit at the gateway. |
| 10 | **No notifications.** | Members are not told when a hold is about to expire or a booking is cancelled. | Out of scope. |
| 11 | **No data retention or privacy policy.** | Names and emails are kept forever. | Decide with the client (Q-14). |

## 3. Behaviours that can surprise people

| # | Limitation | What happens | Notes |
|---|---|---|---|
| 12 | **A recurring series counts as one booking toward the cap of 3.** **By design.** | One member can hold up to 3 series × 12 weeks = **36 future slot-hours**, and one member can lock a prime slot for 12 weeks. Cancelling one week does not free any cap; only cancelling every live week does. | See DESIGN §14. Client question Q-1: should there be a ceiling on total future slots? |
| 13 | **Retrying a lost `POST /recurring-bookings` fails.** | If the first request succeeded but its response was lost, the retry returns `409 series_conflict` against the member's own new bookings. The bookings exist; the member must look at the court's availability, or cancel them one by one, until series cancel exists. | We cut idempotency keys to keep scope. Single holds, confirms and cancels *are* safe to retry. Week 4: series cancel. |
| 14 | **A series cannot be cancelled in one call yet.** | Cancelling a 12-week series takes 12 requests. | Planned for week 4. |
| 15 | **Confirming an expired hold fails even if the slot is still free.** **By design.** | The member must hold again, and could lose the slot to someone else in between. | DESIGN BR-7. |
| 16 | **`GET /courts/{id}/availability` is advisory.** | A slot shown `AVAILABLE` can be taken before the member holds it. Only a hold reserves a slot. `HELD` slots may free up within minutes. | DESIGN BR-19. |
| 17 | **Only one-hour slots, one daily opening time per venue, no holidays.** | Venues cannot close on holidays, have weekend hours, or sell 2-hour games. | Questions Q-4, Q-5, Q-6. |
| 18 | **Clock changes (DST).** | A weekly series keeps its local wall-clock time. A series that would land on a local time that does not exist (the skipped hour in spring) is rejected as `invalid_slot`. In autumn the repeated hour is offered **once**; its second occurrence cannot be booked. | Tested with fixed dates in `tests/unit/test_weeks_and_series.py` and the utilization tests. |
| 19 | **Utilization is "confirmed so far", not "played".** | It counts confirmed bookings in the week, including ones still in the future. No-shows are not tracked. A booking cancelled late (no refund) is *not* counted as used. | Question Q-10. |
| 20 | **Utilization week is Monday–Sunday in each venue's own time zone.** | Two venues in different zones are reported for different UTC ranges under the same week label. | Intended. |

## 4. Housekeeping risks in the database

| # | Limitation | What happens | Fix |
|---|---|---|---|
| 21 | **Expired holds are never cleaned up.** | Their rows stay `status = 'HELD'` forever. Every read the system makes ignores them (the `live_bookings` view, the expiry trigger), so bookings, caps and availability are correct, but the table grows, and **any new query or report that reads raw `status` will wrongly count expired holds as live**. Always read `live_bookings`. | An operator can run the one-line cleanup in TRD §9 at any time; a scheduled sweeper was cut as unnecessary for correctness. |
| 22 | **The `live_bookings` view is `SELECT *`.** | Adding a column to `bookings` does not appear in the view until the view is recreated. | Recreate the view in the same migration that adds a column. |
| 23 | **Only one migration exists and downgrade has not been exercised.** | Rolling back is untested. | Test `alembic downgrade` before the first schema change. |
| 24 | **Behaviour lives in the database (trigger, constraint, view).** | Someone who restores the data into a database without the migration loses the guarantees silently. | The tests build their database by running the real migration, so a missing constraint fails the tests; never restore data without the schema objects. |

## 5. Limits of the tests

- **No fake clock.** Time comes only from the database, so tests back-date rows to test expiry; the exact 24-hour boundary is checked by unit tests on the pure function and by HTTP tests with a 5-minute margin, not by an HTTP test at exactly 24 h.
- **The expiry race at the last instant** (confirm one microsecond before expiry versus a new hold) is argued from Postgres row-lock behaviour (TRD §6.2), and the confirm-versus-takeover race is exercised, but the exact microsecond cannot be forced.
- **Mutation checks were done by hand** (we removed the constraint, the trigger and the member lock and watched the matching tests fail). They are not automated.
- **CI ran on one Linux runner.** Windows and macOS are covered only by the README's own notes.
