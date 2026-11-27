# CourtLock — Design Document (PRD)

| | |
|---|---|
| **Status** | Draft **v0.2** (design accepted in review). **Weeks 2 and 3 are built**: see README and TRD v0.4. Series cancel, load measurements and the final LIMITATIONS.md are week 4. |
| **Scope of this doc** | *What* we build and *why*: requirements, assumptions, open questions, rejected alternatives. The *how* lives in [docs/TRD.md](docs/TRD.md). |
| **Project name** | **CourtLock** — a court is locked to one booking at a time; that is exactly the guarantee this system makes. Sport-neutral, since the club rents courts for all kinds of sports. |

Other name candidates considered: *Tiebreak* (reads as tennis-only, rejected), *Baseline*, *Hardcourt*, *Slotwise*.

---

## 0. Response to review (v0.1 → v0.2)

| # | Feedback | Verdict | What changed |
|---|---|---|---|
| 1 | Walk `POST /recurring-bookings` through a slot with an **expired but unswept hold**. | **v0.1 was broken.** Expiry-eviction lived only inside `create_hold()`. Recurring has its own batch insert, so it would hit the stale `HELD` row, fail the exclusion constraint, and return a false `series_conflict` for a slot that is logically free — violating BR-6 for series bookings. | Eviction is no longer application code on one path. It is a **`BEFORE INSERT` trigger on `bookings`**, so *every* writer (hold, recurring, a future maintenance block, a hand-typed `psql` insert) evicts expired holds before the constraint is checked. All reads of "what is blocking a slot" go through one view (`live_bookings`). Walk-through in TRD §6.2. Tests T-R1…T-R5 in TRD §11. |
| 2 | Q-1 default makes a 12-week series impossible under the cap. | **Agreed.** A weekly recurring booking that can last 3 weeks is not what a client means. | **A recurring series counts as ONE booking against the cap** (BR-10). Cost stated plainly in §14: one member can now hold up to 3 × 12 = 36 future slot-hours instead of 3. |
| 3 | Cut scope; **all four contract endpoints in week 2.** | **Agreed.** | §3 scope tiers: week 2 = the 4 endpoints + pricing + the three guarantees, proven. Cut: idempotency-key table, background sweeper, venue/court/member CRUD, `/me/bookings`, `DELETE /holds`, fake clock, fault hooks in production code, metrics. |
| 4 | "Database enforces everything" vs. app clock for expiry and the 24 h rule. | **Changed.** Justifying it would not survive two API instances with different clocks. | **One clock: the database's.** The app never supplies "now" (BR-22). Expiry is evaluated *inside* Postgres (trigger, view, conditional `UPDATE`); the 24 h rule uses `clock_timestamp()` read inside the transaction after the row lock. The principle is restated honestly in TRD §1: invariants are enforced by the DB; time comes from the DB; policy numbers (24 h, 3, 5 min) live in app code. |
| 5 | README: every request/response, copy-pasteable `curl`, seeded demo members, identity in one place. | **Done** (README rewritten). Shapes in TRD §7 are accepted as the contract (**Q-9 closed**). | README is the contract the graders call. CI will execute the README's curl blocks against `docker compose up` so docs cannot drift from behaviour. Identity lives in one module (`api/deps.py`). |

---

## 1. Problem

A community club rents sports courts by the hour across a few venues. The #1 complaint today is **double-booking**: two members turn up for the same court at the same time. We are building the backend that makes that impossible, and that can *prove* it is impossible.

## 2. Goals

| ID | Goal |
|---|---|
| G1 | **A court is never double-booked**, no matter how many requests arrive at once. |
| G2 | **A client retrying a timed-out confirm never ends up with two bookings** (or two charges). |
| G3 | **A failed multi-step operation leaves no partial state.** |
| G4 | Members can browse availability, reserve a slot briefly, confirm it, and cancel it. |
| G5 | Members can book a court weekly (recurring). |
| G6 | Venues price peak and off-peak hours differently. |
| G7 | Staff can see court utilization per week. |
| G8 | Every guarantee G1–G3 has an automated test that would fail if the guarantee were broken. |

## 3. Scope — what we prove first, and what we cut

### 3.1 Tiers

| Tier | Contents | When |
|---|---|---|
| **Core** | The 4 fixed endpoints; schema + constraint + expiry trigger + `live_bookings` view; pricing (peak/off-peak); member cap (3); refund rule; seed data; compose (`db`, `migrate`, `api`); tests proving **G1, G2, G3** for these endpoints; README with curl; minimal CI. | **Week 2** |
| **Required by the brief** | `POST /recurring-bookings` (+ its G1/G3 tests, incl. the expired-hold walk-through); `GET /staff/utilization`. | Week 3 |
| **Should** | `GET /courts` (so members can discover court ids), `POST /recurring-bookings/{id}/cancel` (cancel a whole series atomically), DST edge tests, Hypothesis stateful test. | Week 3–4 if time |
| **Cut (not built)** | see §3.2 | — |

### 3.2 Cut list

| Cut | Why it is safe | Cost we accept |
|---|---|---|
| **Idempotency-Key header + table** | Confirm and cancel are idempotent by construction (state machine on one row). A retried `POST /holds` returns the member's own live hold (BR-5). That covers every retry the brief asks about. | Retrying a *recurring* create after a lost response gets `series_conflict` (it conflicts with its own bookings). Documented in LIMITATIONS. |
| **Background sweeper / worker** | Correctness of expiry never depended on it (§0 #1: it is DB-side now). | Expired holds stay in `HELD` state forever in the table (harmless: every read ignores them). Table grows slowly; a one-line cleanup SQL is documented. One fewer compose service, one fewer failure mode. |
| **Venue / court / member CRUD, `/venues`, `/me/bookings`** | Brief needs browse-availability and booking, not catalogue admin. Seed script creates data. | Adding a court means editing seed data / SQL. |
| **`DELETE /holds/{id}`** | `POST /bookings/{id}/cancel` on a live hold releases it (BR-14). | None. |
| **App-side fake clock; fault hooks inside production code** | DB clock + back-dated rows for time tests; test-only DB triggers for failure injection (TRD §11). | Exact 24 h boundary is unit-tested on the pure function, not over HTTP. |
| **Structured JSON logging, request ids, metrics, `/readyz`** | Not asked for. Plain logging + `/healthz`. | Harder to operate in production. |
| **Per-weekday opening hours, holidays, multi-hour bookings** | Open questions Q-5, Q-6, Q-4. | Sold as "later". |

## 4. Non-goals (v1)

Real payment gateway, login/sign-up, UI, notifications, waitlists, tournaments, staff editing bookings on behalf of members, multi-region.

## 5. Users

| Persona | Needs |
|---|---|
| **Member** | See what is free, grab a slot without someone snatching it mid-checkout, cancel if plans change, set up a regular weekly game. |
| **Staff** | See how busy each court/venue is week by week. |
| **Club owner** | No more double-booking complaints; refunds follow the stated policy exactly. |

## 6. Business rules (decisions)

Where the brief is silent or contradicts itself we **decide, write it down, and apply it everywhere**. Tests and code reference these ids.

| ID | Rule |
|---|---|
| BR-1 | A **slot** is exactly **one hour**, starting on the hour **in the venue's local time zone** (so a venue in `Asia/Kolkata` has slots at :00 IST = :30 UTC). |
| BR-2 | A booking is **one court × one slot**. |
| BR-3 | Each venue has **opening hours** (one daily open/close, local). Only slots fully inside them exist. |
| BR-4 | A slot can be held from now until **90 days ahead** (configurable); a slot that has started cannot be held or booked. For a recurring series the **first** occurrence must satisfy this; later weeks may extend up to 12 weeks beyond it. |
| BR-5 | **Two-step booking.** `POST /holds` reserves a slot for a short TTL (**5 min**, configurable): `hold_expires_at = min(created + TTL, slot start)`. `POST /holds/{id}/confirm` turns it into a booking. **Retry rule:** if the same member repeats `POST /holds` for the same slot while their hold is still live, they get that hold back (`200`), not an error. |
| BR-6 | **An unconfirmed hold stops blocking the slot the instant it expires — for every writer, on every code path — whether or not any cleanup has run.** Enforced in the database (TRD §6.2). |
| BR-7 | Confirming an **expired** hold fails (`hold_expired`) even if the slot is still free. The member must hold again. |
| BR-8 | **Price is locked at hold time** (band and amount snapshotted). Confirm charges exactly that. |
| BR-9 | **Pricing:** each venue has a peak and an off-peak price per hour (integer minor units) and weekly **peak windows** (weekday + local start/end). A slot is *peak* if its local start falls in a window, else *off-peak*. |
| BR-10 | **Cap: a member has at most 3 active future bookings.** A *booking* for this rule is **either one standalone booking/hold, or one whole recurring series**. A series counts **once** while it has at least one live future occurrence. Unexpired holds count. *(Decision for Q-1 — see §14 for what it costs.)* |
| BR-11 | **Refund:** if `start_at − now ≥ 24 h` → **100 %**, else **0 %**. Exactly 24 h counts as "at least". |
| BR-12 | A booking that has **already started cannot be cancelled.** |
| BR-13 | Cancelling frees the slot immediately, whatever the refund. |
| BR-14 | **Cancel is idempotent**: cancelling an already-cancelled booking returns the original result and never refunds twice. Cancelling a **live hold** releases it (no money moved, refund 0). |
| BR-15 | **Confirm is idempotent**: confirming an already-confirmed hold (by its owner) returns the same booking; never charges twice. |
| BR-16 | **Recurring booking:** member picks court, first start time and number of weeks (2–12). Occurrences are the same **local** wall-clock time every 7 days. The system books **all occurrences or none**. Each occurrence is an ordinary booking, charged up front, and **cancelled/refunded individually** by BR-11. |
| BR-17 | Recurring skips the hold step (created and confirmed atomically) but obeys slot, cap and horizon rules. |
| BR-18 | **Utilization** (court, week) = confirmed booked hours ÷ bookable hours. Bookable hours = court's opening hours in that Mon–Sun venue-local week. Cancelled bookings and holds are **not** counted. |
| BR-19 | **Availability is advisory; the hold is the guarantee.** |
| BR-20 | Availability shows `HELD` / `BOOKED` **without revealing who**. |
| BR-21 | Money is integer minor units + currency; no floats. |
| BR-22 | **Time source = the database clock.** Expiry, the 24 h rule, "slot has started" and the horizon are all evaluated against Postgres time. The application never supplies "now" to a business decision. |
| BR-23 | **"Live" has one definition:** a booking is *live* if it is `CONFIRMED`, or `HELD` and not past `hold_expires_at`. Everything that asks "what blocks this slot?" or "what counts toward the cap?" uses that definition (a single DB view). |

## 7. Functional requirements

| ID | Endpoint / feature | Tier | Acceptance criteria |
|---|---|---|---|
| FR-1 | `POST /holds` | Core | Free slot → `201` hold with locked price + `expires_at`. Same member repeats while live → `200` same hold. Taken slot → `409 slot_unavailable`. 4th active booking → `409 booking_limit_reached`. Outside hours/past/beyond horizon → `422 invalid_slot`. **100 concurrent requests for one slot → exactly one `201`, rest `409`.** An expired hold on the slot does not block it. |
| FR-2 | `POST /holds/{id}/confirm` | Core | Within TTL → `200` `CONFIRMED` booking (`id` = hold id). Repeating it any number of times, sequentially or concurrently → `200`, same body, **one** ledger charge. After TTL → `409 hold_expired`. Someone else's hold → `404`. |
| FR-3 | `POST /bookings/{id}/cancel` | Core | ≥ 24 h → refund 100 %; < 24 h → 0 %; repeat → same stored result, ≤ 1 refund ever; started → `409 booking_started`; live hold → released, refund 0. |
| FR-4 | `GET /courts/{id}/availability` | Core | Every slot in the venue-local date range with `AVAILABLE` / `HELD` / `BOOKED` + price. Expired holds show `AVAILABLE`. No auth. |
| FR-5 | `POST /recurring-bookings` | Required — **built (wk 3)** | All-or-nothing (BR-16). Series spanning a slot with an **expired unswept hold** succeeds. A live conflict → `409 series_conflict` listing conflicting dates; **zero** rows written. Counts once toward cap. |
| FR-6 | `GET /staff/utilization?week=YYYY-Www[&venue_id=]` | Required — **built (wk 3)** | Per court and per venue (BR-18), with peak/off-peak split. Staff key required. |
| FR-7 | `GET /courts` | Should — **built (wk 3)** | List active courts with venue, sport, timezone. |
| FR-8 | `POST /recurring-bookings/{id}/cancel` | Should — week 4 | Cancel all future occurrences atomically; each refunded per BR-11. |

## 8. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-1 | Correctness over throughput. Informal target: p95 < 200 ms for hold/confirm/cancel at ~100 req/s on laptop-class Postgres. |
| NFR-2 | G1–G3 hold with **several API instances** against one Postgres, i.e. they are enforced in the database. |
| NFR-3 | `docker compose up` yields a migrated, seeded, working system with no manual steps. |
| NFR-4 | Python 3.11, FastAPI, SQLAlchemy 2 + Alembic, Postgres 15, pytest. `ruff` and `mypy --strict` clean with **zero inline suppressions**; CI fails if any appear. |
| NFR-5 | Errors are `problem+json` with a stable `code`. |
| NFR-6 | **Identity handling lives in exactly one module** (`api/deps.py`) so changing the scheme is a one-file edit. |
| NFR-7 | **The README is executable:** CI runs its curl examples against the compose stack. |

## 9. Assumptions

1. One club, a few venues (≈ 3–10), a handful of courts each. We optimise for provable correctness, not scale.
2. **Identity is upstream.** A trusted gateway authenticates and passes `X-Member-Id`; staff send `X-Staff-Key`. No login is built. Isolated in `api/deps.py`; listed in LIMITATIONS.
3. **Payment is an internal ledger** (append-only charge/refund rows). Real money movement is someone else's job.
4. One currency per venue; no conversion.
5. **One Postgres primary.** Its clock is the system's only clock (BR-22).
6. Timestamps stored as UTC `timestamptz`; venue time zone only defines slot boundaries, opening hours, peak windows, and "week".
7. Members exist (seeded). Member id = UUID.
8. The cap counts a member's own bookings across all venues.
9. Staff read; they do not modify bookings in v1.

## 10. Contradictions & gaps in the brief — and what we decided

| # | Gap / tension | Decision |
|---|---|---|
| C-1 | "At most 3 active future bookings" vs "weekly recurring booking". | A series counts **once** (BR-10). Cost in §14. |
| C-2 | "Full refund" but no payment in the brief. | Internal ledger; charge on confirm, refund on cancel. |
| C-3 | Does a hold count toward the cap? | Yes, live holds count; else one member could hoard slots by cycling holds. |
| C-4 | Fixed endpoints, no shapes. | We define them (TRD §7, README). **Accepted by the client (Q-9 closed).** |
| C-5 | How long is "briefly"? | 5 min, configurable. |
| C-6 | Is exactly 24 h "at least 24 h"? | Yes. |
| C-7 | Confirm after expiry when the slot is still free. | Reject (BR-7): soft expiry is hard to test and reason about. |
| C-8 | Recurring: one step or two? | One atomic step (BR-17). N holds would count against the cap and could fail halfway. |
| C-9 | Utilization denominator. | Opening hours, not 24 h. |
| C-10 | Cancel after start? | No. |
| C-11 | Which zone defines peak and "week"? | Venue-local. |
| C-12 | Recurring across DST: same UTC time or same wall-clock time? | Same **wall-clock** time in the venue zone (a 10:00 game stays at 10:00). |
| C-13 | Who is "the clock"? | The database (BR-22). |

## 11. Open questions for the client

| # | Question | Our default |
|---|---|---|
| Q-1 | ~~Recurring series vs the 3-booking cap~~ **Decided:** a series counts once. Please confirm; and should there also be a ceiling on total future slots (e.g. 36)? | Series = 1. No secondary ceiling. |
| Q-2 | Is **payment** in scope (gateway, charge at venue, deposits)? | Internal ledger only. |
| Q-3 | Hold length; may members extend? | 5 min, no extension. |
| Q-4 | **Multi-hour** bookings? | 1 h only. Schema uses a time range so it is additive. |
| Q-5 | Different opening hours per weekday? | One daily range per venue. |
| Q-6 | Public holidays / special pricing / closures? | None. |
| Q-7 | Recurring with one conflicting week: reject all, or book the free weeks? | Reject all. |
| Q-8 | Should staff cancel/move bookings or block courts for maintenance? | No. (Blocks would reuse the same non-overlap constraint.) |
| ~~Q-9~~ | ~~Request/response shapes~~ | **Closed — our shapes accepted.** |
| Q-10 | Is a late-cancelled (no-refund) slot "utilised"? | No: cancelled = not utilised. |
| Q-11 | What happens to bookings if a court is retired? | Not handled. |
| Q-12 | Minimum lead time before a slot? | None beyond "in the future". |
| Q-13 | Expected peak load? | Tens of requests/second. |
| Q-14 | Retention / privacy? | Keep forever; name + email only. |
| Q-15 | If a member's recurring request is retried after a lost response, is a `series_conflict` with *their own* bookings acceptable? | Yes in v1 (idempotency key cut). |

## 12. Alternatives rejected (product/design level)

| Alternative | Why rejected |
|---|---|
| Single-step booking (no hold). | The brief requires reserve-then-confirm. |
| Holds that expire only when a cron/worker marks them. | Violates "never block forever" the moment the job is down. Expiry is a DB property, not a job's. |
| **Eviction in application code on each insert path** *(our v0.1)* | A new path (recurring) forgot it. Replaced by a trigger so no path can forget (§0 #1). |
| Confirm after expiry if slot still free. | Soft expiry is hard to reason about. |
| Virtual recurring occurrences ("rule" rows). | The non-overlap guarantee would stop being a DB constraint. |
| Partial success on recurring series. | Violates "no partial state" in spirit; confusing refunds. (Q-7) |
| **Counting every occurrence toward the cap** *(our v0.1)* | Caps a series at 3 weeks; not what "weekly recurring" means to a client. |
| Counting only confirmed bookings toward the cap. | Lets one member block many slots through repeated holds. |
| Refund tiers. | Brief says full-or-none. |
| Client-supplied price at confirm. | Never trust client money. |
| Application clock as source of time. | Skew between instances; contradicts "DB enforces". |

## 13. Success metrics

- **0 double-bookings** under the concurrency suite (≥ 100 concurrent requests per slot, repeated rounds) and under a direct-SQL attack on the constraint.
- **0 duplicate ledger rows** when confirm/cancel are replayed ≥ 20 times, sequentially and concurrently.
- **100 %** of injected mid-operation failures leave every table identical to its prior state.
- **Expired-unswept-hold tests pass on every insert path** (hold, recurring, raw SQL).
- CI green: ruff, ruff format, mypy --strict, pytest, compose smoke + README examples.

## 14. Delivery plan, risks, and costs

### Plan

| Phase | Output | Exit criterion |
|---|---|---|
| **Week 1** | This PRD, TRD, README. | Reviewed (done once; v0.2 is the re-review). |
| **Week 2** ✅ | **All four endpoints** under `docker compose up`; schema, trigger, view; pricing; cap; refund; seed; G1/G2/G3 tests; README examples running in CI. | A reviewer can run every README curl and the guarantee tests are green. |
| **Week 3** ✅ | Recurring (+ walk-through tests T-R1…T-R5), utilization (incl. clock-change weeks), `GET /courts`, draft LIMITATIONS.md. | Brief's feature list complete. **Done.** |
| **Week 4** | Series cancel, load check against NFR-1, stateful property test, LIMITATIONS.md final, walkthrough rehearsal (TRD §14). | — |

### Costs of the decisions (stated, not hidden)

| Decision | Cost |
|---|---|
| **Series counts once (BR-10).** | A member can hold up to **3 series × 12 weeks = 36 future slot-hours** instead of 3, so one member can lock a prime Saturday slot for 12 weeks (arguably what a "standing booking" is for). The cap no longer bounds how many courts-hours a member occupies, only how many *commitments*. Mitigations: series ≤ 12 weeks, 90-day first-slot horizon, and a possible secondary ceiling (Q-1). Cancelling one occurrence does **not** free cap; only cancelling all future occurrences does, so without `FR-8` that is up to 12 calls. |
| **DB clock (BR-22).** | Time-dependent behaviour cannot be tested with a fake clock; we back-date rows and unit-test the pure functions instead. One DB primary is a single source of truth *and* a single point of failure. A step change of the DB clock (bad NTP) would shift expiry and the 24 h rule. |
| **No sweeper.** | `HELD` rows pile up; correctness unaffected; `bookings` grows. |
| **Trigger-based eviction.** | Business behaviour in plpgsql is less visible than Python. Mitigation: ~10 lines, covered by raw-SQL tests, described in TRD §6.2. |
| **No idempotency key.** | See §3.2. |

### Risks

| Risk | Mitigation |
|---|---|
| README examples and API drift apart (graders use the README). | CI executes the README curls (NFR-7). |
| Deadlocks between concurrent recurring requests. | Insert in ascending `(court_id, start_at)`; bounded retry on `40P01` (TRD §6.5); concurrency test. |
| Scope creep into payments/auth/UI. | §4 non-goals; §3 tiers. |
