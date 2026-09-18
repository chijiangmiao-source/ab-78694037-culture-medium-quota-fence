-- Initial schema for the culture-medium quota service.
-- All quantities are integer millilitres. Every deadline/timestamp is
-- produced by the database clock (now()); the application never supplies
-- wall-clock time for lease boundaries.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS batch (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    total_ml       INTEGER NOT NULL,
    available_ml   INTEGER NOT NULL,
    reserved_ml    INTEGER NOT NULL DEFAULT 0,
    confirmed_ml   INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_batch_total_positive CHECK (total_ml > 0),
    CONSTRAINT chk_batch_balances_non_negative CHECK (
        available_ml >= 0 AND reserved_ml >= 0 AND confirmed_ml >= 0
    ),
    -- The conservation law enforced at every commit:
    -- total = available + valid reservations + confirmed.
    CONSTRAINT chk_batch_conservation CHECK (
        total_ml = available_ml + reserved_ml + confirmed_ml
    )
);

CREATE TABLE IF NOT EXISTS reservation (
    -- The identity sequence makes fence tokens strictly increasing and
    -- never reusable, including for cancelled/expired rows. Because the
    -- sequence is global, tokens are also strictly increasing inside one
    -- batch (gaps are allowed, reuse never is).
    fence_token    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id       BIGINT NOT NULL REFERENCES batch(id),
    amount_ml      INTEGER NOT NULL,
    status         TEXT NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at   TIMESTAMPTZ,
    cancelled_at   TIMESTAMPTZ,
    CONSTRAINT chk_reservation_amount_positive CHECK (amount_ml > 0),
    CONSTRAINT chk_reservation_status CHECK (
        status IN ('held', 'confirmed', 'cancelled', 'expired')
    ),
    CONSTRAINT chk_reservation_confirmed_at CHECK (
        (status = 'confirmed') = (confirmed_at IS NOT NULL)
    ),
    CONSTRAINT chk_reservation_cancelled_at CHECK (
        (status = 'cancelled') = (cancelled_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_reservation_batch_held
    ON reservation (batch_id)
    WHERE status = 'held';

CREATE INDEX IF NOT EXISTS ix_reservation_batch_status
    ON reservation (batch_id, status);

-- A batch total is immutable once the batch exists. Enforce it in the
-- database so no code path (or manual connection) can rewrite total_ml.
CREATE OR REPLACE FUNCTION fn_batch_total_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.total_ml IS DISTINCT FROM OLD.total_ml THEN
        RAISE EXCEPTION 'batch total_ml is immutable after creation'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_batch_total_immutable ON batch;
CREATE TRIGGER trg_batch_total_immutable
    BEFORE UPDATE ON batch
    FOR EACH ROW
    EXECUTE FUNCTION fn_batch_total_immutable();

INSERT INTO schema_migrations (version)
VALUES ('0001_init')
ON CONFLICT (version) DO NOTHING;
