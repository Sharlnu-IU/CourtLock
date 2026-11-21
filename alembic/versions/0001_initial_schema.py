"""Initial schema: tables, double-booking constraint, expiry trigger, live view.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_SQL = """
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TYPE booking_status AS ENUM ('HELD', 'CONFIRMED', 'CANCELLED', 'EXPIRED');
CREATE TYPE price_band AS ENUM ('PEAK', 'OFFPEAK');

CREATE TABLE venues (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    timezone text NOT NULL,
    opens_at time NOT NULL,
    closes_at time NOT NULL,
    peak_price_cents integer NOT NULL CHECK (peak_price_cents >= 0),
    offpeak_price_cents integer NOT NULL CHECK (offpeak_price_cents >= 0),
    currency char(3) NOT NULL,
    CHECK (opens_at < closes_at)
);

CREATE TABLE peak_windows (
    id uuid PRIMARY KEY,
    venue_id uuid NOT NULL REFERENCES venues (id),
    weekday smallint NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time time NOT NULL,
    end_time time NOT NULL,
    CHECK (start_time < end_time)
);

CREATE TABLE courts (
    id uuid PRIMARY KEY,
    venue_id uuid NOT NULL REFERENCES venues (id),
    name text NOT NULL,
    sport text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    UNIQUE (venue_id, name)
);

CREATE TABLE members (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    email text NOT NULL UNIQUE
);

CREATE TABLE recurring_series (
    id uuid PRIMARY KEY,
    member_id uuid NOT NULL REFERENCES members (id),
    court_id uuid NOT NULL REFERENCES courts (id),
    weeks smallint NOT NULL CHECK (weeks BETWEEN 2 AND 12),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE bookings (
    id uuid PRIMARY KEY,
    member_id uuid NOT NULL REFERENCES members (id),
    court_id uuid NOT NULL REFERENCES courts (id),
    series_id uuid REFERENCES recurring_series (id),
    start_at timestamptz NOT NULL,
    end_at timestamptz NOT NULL,
    during tstzrange GENERATED ALWAYS AS (tstzrange(start_at, end_at, '[)')) STORED,
    status booking_status NOT NULL,
    band price_band NOT NULL,
    price_cents integer NOT NULL CHECK (price_cents >= 0),
    currency char(3) NOT NULL,
    hold_expires_at timestamptz,
    confirmed_at timestamptz,
    cancelled_at timestamptz,
    refund_percent smallint CHECK (refund_percent BETWEEN 0 AND 100),
    refund_cents integer CHECK (refund_cents >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (end_at = start_at + interval '1 hour'),
    CHECK (status <> 'HELD' OR hold_expires_at IS NOT NULL),
    CHECK (status <> 'CONFIRMED' OR confirmed_at IS NOT NULL),
    CHECK (status <> 'CANCELLED'
           OR (cancelled_at IS NOT NULL AND refund_percent IS NOT NULL
               AND refund_cents IS NOT NULL)),
    -- G1: the double-booking guarantee.
    CONSTRAINT bookings_no_overlap
        EXCLUDE USING gist (court_id WITH =, during WITH &&)
        WHERE (status IN ('HELD', 'CONFIRMED'))
);

CREATE INDEX bookings_member_future_idx ON bookings (member_id, start_at)
    WHERE status IN ('HELD', 'CONFIRMED');
CREATE INDEX bookings_court_time_idx ON bookings (court_id, start_at);

-- G2: at most one charge and one refund per booking.
CREATE TABLE ledger_entries (
    id uuid PRIMARY KEY,
    booking_id uuid NOT NULL REFERENCES bookings (id),
    member_id uuid NOT NULL REFERENCES members (id),
    kind text NOT NULL CHECK (kind IN ('CHARGE', 'REFUND')),
    amount_cents integer NOT NULL CHECK (amount_cents > 0),
    currency char(3) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (booking_id, kind)
);

-- BR-6: an expired hold stops blocking its slot for EVERY writer, on every code path.
-- The exclusion constraint cannot mention now(), so the table evicts stale holds itself.
-- NEW.during is not computed yet inside a BEFORE trigger, hence the explicit tstzrange.
CREATE FUNCTION evict_expired_holds() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IN ('HELD', 'CONFIRMED') THEN
        UPDATE bookings
           SET status = 'EXPIRED'
         WHERE court_id = NEW.court_id
           AND during && tstzrange(NEW.start_at, NEW.end_at, '[)')
           AND status = 'HELD'
           AND hold_expires_at <= clock_timestamp();
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER bookings_evict_expired
    BEFORE INSERT ON bookings
    FOR EACH ROW EXECUTE FUNCTION evict_expired_holds();

-- BR-23: the one definition of "live".
CREATE VIEW live_bookings AS
SELECT * FROM bookings
 WHERE status = 'CONFIRMED'
    OR (status = 'HELD' AND hold_expires_at > clock_timestamp());
"""

DOWNGRADE_SQL = """
DROP VIEW live_bookings;
DROP TRIGGER bookings_evict_expired ON bookings;
DROP FUNCTION evict_expired_holds();
DROP TABLE ledger_entries;
DROP TABLE bookings;
DROP TABLE recurring_series;
DROP TABLE members;
DROP TABLE courts;
DROP TABLE peak_windows;
DROP TABLE venues;
DROP TYPE price_band;
DROP TYPE booking_status;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
