# CourtLock — Design Document (PRD)

| | |
|---|---|
| **Status** | Draft v0.1 — for review before any build work |
| **Scope of this doc** | *What* we build and *why*: product requirements, assumptions, open questions, rejected alternatives. The *how* lives in [docs/TRD.md](docs/TRD.md). |
| **Project name** | **CourtLock** — a court is locked to one booking at a time; that is exactly the guarantee this system makes. Sport-neutral, since the club rents courts for all kinds of sports. |

Other name candidates considered: *Tiebreak* (reads as tennis-only, rejected), *Baseline*, *Hardcourt*, *Slotwise*.

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

## 3. Non-goals (v1)

- Real payment-gateway integration (we keep an internal ledger; see D-07).
- Member sign-up, login, password reset, or any UI. Identity is supplied by an upstream layer (see D-02).
- Notifications (email/SMS/push).
- Waitlists, tournaments, coaching sessions, equipment rental.
- Multi-hour bookings, partial-hour bookings.
- Staff overriding/cancelling on behalf of members.
- Multi-region / multi-writer database deployment.

## 4. Users

| Persona | Needs |
|---|---|
| **Member** | See what is free, grab a slot without someone else snatching it mid-checkout, cancel if plans change, set up a regular weekly game. |
| **Staff** | Know how busy each court/venue is, week by week, to plan maintenance and pricing. |
| **Club owner** (stakeholder) | No more double-booking complaints; refunds follow the stated policy exactly. |

## 5. Business rules (decisions)

Where the brief is silent or contradicts itself we **decide, write it down, and apply it everywhere**. Each rule has an ID that tests, code and docs reference.

| ID | Rule | Notes |
|---|---|---|
| BR-1 | A **slot** is exactly **one hour**, starting on the hour **in the venue's local time zone**. | e.g. Asia-Kolkata venue: slots start at :00 IST. |
| BR-2 | A booking is **one court × one slot**. | Multi-hour bookings are a stretch / open question Q-4. |
| BR-3 | Each venue has **opening hours** (one daily open/close time, local). Only slots fully inside opening hours exist. | Per-weekday hours = Q-5. |
| BR-4 | A slot can be booked from **now** until **90 days ahead** (configurable). Slots that have started cannot be held or booked. | |
| BR-5 | **Two-step booking.** Step 1 `POST /holds` reserves a slot for a short **TTL (default 5 min)**. Step 2 `POST /holds/{id}/confirm` turns it into a booking. | |
| BR-6 | An **unconfirmed hold expires** at its TTL and from that instant no longer blocks the slot — even if no cleanup job has run. | Correctness never depends on a background job (see TRD §6.2). |
| BR-7 | Confirming an **expired** hold fails (`hold_expired`), even if the slot is still free. The member must hold again. | Strict and predictable; alternative rejected in §9. |
| BR-8 | **Price is locked at hold time.** Confirm charges exactly the quoted price, even if the price list or the clock crossed a peak boundary meanwhile. | |
| BR-9 | **Pricing:** each venue has a peak price and an off-peak price per hour (integer minor units, e.g. cents) and a set of weekly **peak windows** (weekday + local start/end). A slot is *peak* if its local start time falls in a peak window, else *off-peak*. | Public holidays = Q-6. |
| BR-10 | **Cap:** a member may have at most **3 active future bookings**. "Active future" = `CONFIRMED` with start in the future, **plus unexpired holds** (a hold is a claim on a slot; counting it stops hoarding). | Configurable. |
| BR-11 | **Cancellation refund:** if `start_at − now ≥ 24 h` → **100 % refund**, otherwise **0 %**. Exactly 24 h counts as "at least 24 h" (full refund). | Measured against the slot start, using the server clock. |
| BR-12 | A booking that has **already started cannot be cancelled.** | |
| BR-13 | Cancelling frees the slot immediately for others, regardless of refund amount. | |
| BR-14 | **Cancel is idempotent**: cancelling an already-cancelled booking returns the original result (same refund) and never refunds twice. | |
| BR-15 | **Confirm is idempotent**: confirming an already-confirmed hold (by its owner) returns the same booking and never charges twice. | |
| BR-16 | **Recurring booking:** a member picks court + weekday + local start time + number of weeks (2–12). The system books **all occurrences or none**. Each occurrence is an ordinary booking that is paid for, counted against the cap (BR-10), and cancelled/refunded **individually** by BR-11. | Consequence: with a cap of 3, a series is at most 3 weeks long unless the member has fewer other bookings. See Q-1 — this is the most important open question. |
| BR-17 | Recurring bookings skip the hold step (they are created and confirmed atomically), but still obey slot, cap and horizon rules. | |
| BR-18 | **Utilization** for a court/week = `confirmed booked hours ÷ bookable hours`. Bookable hours = the court's opening hours in that week (Mon–Sun, venue-local; DST weeks handled). Cancelled bookings and unexpired holds are **not** counted as booked. | Staff only. |
| BR-19 | **Availability is advisory; the hold is the guarantee.** Seeing a slot as `AVAILABLE` does not reserve it. | |
| BR-20 | Availability shows other members' holds as `HELD` and confirmed bookings as `BOOKED`, **without revealing who**. | Privacy. |
| BR-21 | Money is stored as **integer minor units** plus a currency code; no floats. | |

## 6. Functional requirements

Priority: **M** = must (contract / guarantees), **S** = should, **C** = could.

### 6.1 Contract endpoints (fixed — tests call these)

| ID | Endpoint | Req | Acceptance criteria |
|---|---|---|---|
| FR-1 | `POST /holds` | M | Given a free slot, returns 201 with hold `id`, locked price and `expires_at`. Given a taken slot → 409 `slot_unavailable`. Given 3 active bookings → 409 `booking_limit_reached`. Outside hours / past / beyond horizon → 422. 50 concurrent requests for one slot → exactly one 201, 49 × 409. |
| FR-2 | `POST /holds/{id}/confirm` | M | Within TTL → 200 with a `CONFIRMED` booking whose `id` equals the hold id. Repeating the call any number of times → 200 with the **same** booking, one ledger charge. After TTL → 409 `hold_expired`. Someone else's hold → 404. |
| FR-3 | `POST /bookings/{id}/cancel` | M | ≥ 24 h → refund 100 %; < 24 h → 0 %; repeat → same response, one refund max; already started → 409. |
| FR-4 | `GET /courts/{id}/availability` | M | Returns every slot in the requested local-date range with status `AVAILABLE` / `HELD` / `BOOKED`, plus price and band. Expired holds show `AVAILABLE`. |

Exact request/response shapes: TRD §5. The brief does not give shapes; ours are a decision (Q-9).

### 6.2 Supporting features

| ID | Feature | Req |
|---|---|---|
| FR-5 | `POST /recurring-bookings` — atomic weekly series (BR-16). | M |
| FR-6 | `GET /staff/utilization?week=YYYY-Www[&venue_id=]` — per court and per venue utilization with peak/off-peak split (BR-18). | M |
| FR-7 | `GET /venues`, `GET /venues/{id}/courts` — browse. | S |
| FR-8 | `GET /me/bookings` — a member's own holds and bookings. | S |
| FR-9 | `DELETE /holds/{id}` — voluntarily release a hold. | S |
| FR-10 | `POST /recurring-bookings/{id}/cancel` — cancel all future occurrences atomically; each refunded under BR-11. | C |
| FR-11 | Staff endpoints to create venues, courts, pricing, members (seed script covers v1). | C |
| FR-12 | `Idempotency-Key` header on `POST /holds` and `POST /recurring-bookings` so a retried *hold* is also safe. | S |

## 7. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-1 | Correctness over throughput. Target: p95 < 200 ms for hold/confirm/cancel at 100 req/s on a laptop-class Postgres. Not a hard SLA. |
| NFR-2 | Guarantees G1–G3 hold with **multiple API instances** against one Postgres (so they are enforced in the database, not in app memory). |
| NFR-3 | `docker compose up` yields a working, migrated, seeded system with no manual steps. |
| NFR-4 | Python 3.11, FastAPI, SQLAlchemy 2 + Alembic, Postgres 15, pytest. `ruff` and `mypy --strict` clean with **zero inline suppressions** (`# noqa`, `# type: ignore`, `# fmt: off` …). CI fails if any appear. |
| NFR-5 | Errors are machine-readable (RFC 9457 `problem+json` with a stable `code`). |
| NFR-6 | Structured JSON logs with a request id; every state change logged with booking id. |

## 8. Assumptions

1. **One club**, a few venues (≈ 3–10), each with a handful of courts. Scale is small; we optimise for provable correctness, not horizontal scale.
2. **Identity is upstream.** A trusted gateway authenticates users and passes `X-Member-Id`; staff calls carry `X-Staff-Key`. We do not build login. This is a deliberate stub and is listed in LIMITATIONS.md.
3. **Payment is an internal ledger** (append-only charge/refund rows). "Refund" means a refund ledger entry; money movement is someone else's job.
4. **Currency** is one per venue; no conversion.
5. **Server clock is the source of truth** for hold expiry and the 24 h rule; clocks are NTP-synced. Clients are never trusted for time.
6. **All timestamps** are stored as UTC `timestamptz`; venue time zone is used only to define slot boundaries, opening hours, peak windows and "week".
7. Members already exist (seeded). A member id is a UUID.
8. A member may hold the *same* slot only once, and may hold many different slots up to the cap.
9. The 3-booking cap counts only that member's own bookings, across all venues.
10. Staff can read everything but do not modify bookings in v1.

## 9. Contradictions & gaps in the brief — and what we decided

| # | Gap / tension | Decision |
|---|---|---|
| C-1 | "At most 3 active future bookings" vs "weekly recurring booking" (open-ended by nature). | Each occurrence counts (BR-16), series capped at 12 weeks. Literal, provable, but a series is effectively ≤ 3 weeks. Flagged as Q-1. |
| C-2 | "Full refund" — but the brief never mentions payment. | Internal ledger; charge on confirm, refund on cancel (A-3, D-07). |
| C-3 | Does a *hold* count toward the cap? | Yes, unexpired holds count (BR-10); otherwise one member could hoard every slot for 5 minutes repeatedly. |
| C-4 | Fixed endpoints but no request/response shapes. | We define them (TRD §5) and ask the client (Q-9). |
| C-5 | "Reserve briefly, then confirm" — how long is briefly? | 5 minutes, configurable (BR-5). |
| C-6 | "At least 24 hours' notice" — is exactly 24 h enough? | Yes (BR-11). |
| C-7 | Confirm after hold expiry when the slot is still free. | Reject (BR-7). Reasoning: allowing it makes the "expiry" a soft hint and the race between expiry and takeover harder to reason about. |
| C-8 | Recurring bookings: two-step or one-step? | One atomic step (BR-17). Holding N slots for 5 min each would count against the cap and could fail halfway. |
| C-9 | "Utilization" denominator. | Opening hours, not 24 h (BR-18). |
| C-10 | Can a started booking be cancelled? | No (BR-12). |
| C-11 | Which time zone defines "week" and "peak"? | Venue-local. |

## 10. Open questions we would ask the client

Ordered by how much the answer would change the design.

| # | Question | Our default if unanswered |
|---|---|---|
| Q-1 | Should a recurring series count as **one** toward the 3-booking cap (or each occurrence)? | Each occurrence. If "one", we change one query and add a `series_id` rule; the risk is a member holding dozens of future slots. |
| Q-2 | Is **payment** in scope — which gateway, charge at confirm or at venue, any deposits? | Internal ledger only. A real gateway forces an outbox/saga (TRD §12). |
| Q-3 | How long should a hold last, and may a member extend it? | 5 min, no extension. |
| Q-4 | Do members need **multi-hour** bookings (e.g. 2 h match)? | 1 h only; schema already uses a time range so this is additive. |
| Q-5 | Do venues have **different opening hours per weekday**? | One daily range per venue. |
| Q-6 | **Public holidays / special events** with their own pricing or closures? | None. |
| Q-7 | On a recurring series with one conflicting week: **reject all** or **book the free weeks and report the rest**? | Reject all (atomic). |
| Q-8 | Should **staff** be able to cancel/move bookings, issue goodwill refunds, or block courts for maintenance? | No in v1. Maintenance blocks would reuse the same non-overlap constraint. |
| Q-9 | What shapes do the **test suite's calls** expect (headers for identity, field names, status codes)? | TRD §5. This is the highest-risk assumption for the fixed contract. |
| Q-10 | Is a **no-show** counted as utilization? Do cancelled-late bookings (no refund) count as utilised? | Cancelled = not utilised, regardless of refund. |
| Q-11 | What happens to bookings if a **court is retired** or a venue closes? | Not handled in v1. |
| Q-12 | Is there a **minimum lead time** (e.g. no booking within 1 h of start)? | None; slot must merely be in the future. |
| Q-13 | Expected **peak load** (e.g. Monday-morning booking rush)? | Tens of requests per second. |
| Q-14 | **Data retention / privacy** for member data and bookings? | Keep forever; no PII beyond name + email. |

## 11. Alternatives rejected (product/design level)

| Alternative | Why rejected |
|---|---|
| **Single-step booking** (no hold). | Brief explicitly requires reserve-then-confirm; also avoids charging before the member is sure. |
| **Holds that never expire unless cleaned by a cron job.** | Violates "never block a slot forever" the moment the job is down. We make expiry a property of the *query*, not of a job (TRD §6.2). |
| **Allow confirm after expiry if the slot is still free.** | Soft expiry is hard to test and reason about; one extra tap for the member is a cheap price. |
| **Recurring series as one row that "implies" future bookings** (virtual occurrences). | Double-booking checks would have to reason about rules not rows; the non-overlap guarantee would no longer be a database constraint. |
| **Partial success on recurring series** (book what's free). | Violates "failed multi-step operation leaves no partial state" in spirit; confusing refunds. Kept as Q-7. |
| **Counting only confirmed bookings toward the cap.** | Lets one member block many slots through repeated holds. |
| **Refund tiers** (e.g. 50 % at 12 h). | Brief says full-or-none. |
| **Letting the client pass the price at confirm.** | Never trust client-supplied money. |

## 12. Success metrics

- 0 double-bookings under the concurrency suite (≥ 100 concurrent requests per slot, repeated many rounds) and under a direct-SQL attack on the constraint.
- 0 duplicate ledger rows when confirm/cancel are replayed ≥ 20 times, sequentially and concurrently.
- 100 % of injected mid-operation failures leave the database identical to its prior state (row counts + checksums across all tables).
- CI green: ruff, ruff format, mypy --strict, pytest, compose smoke test.

## 13. Delivery plan

| Phase | Output |
|---|---|
| **Week 1** | This PRD, [TRD](docs/TRD.md), README, reviewed with the client/mentor. **No code until reviewed.** |
| **Week 2** | Schema + migrations, slot/pricing/refund domain logic (pure, unit-tested), hold → confirm → cancel with the DB constraint, concurrency tests. |
| **Week 3** | Availability, recurring, utilization, idempotency keys, sweeper worker, fault-injection tests. |
| **Week 4** | CI, compose polish, LIMITATIONS.md, rehearse the live change (see TRD §14 for the changes we have pre-thought). |

## 14. Risks

| Risk | Mitigation |
|---|---|
| Fixed-contract test shapes differ from ours (Q-9). | Keep identity and body parsing in one thin layer so changing it is a 10-minute edit; ask early. |
| Deadlocks when two recurring requests collide on overlapping slots. | Insert in a deterministic order; bounded automatic retry on deadlock (TRD §6.5). |
| Clock skew between API instances. | Single injectable clock; documented limitation; mitigation option in TRD §13. |
| Scope creep into payments / auth / UI. | Explicit non-goals (§3). |
