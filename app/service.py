"""Quota service core.

Concurrency model (PostgreSQL, READ COMMITTED):

* Every state-changing transaction first runs ``settle_expired_and_lock``:
  it flips ``held`` reservations whose ``expires_at <= now()`` to
  ``expired`` (equality counts as expired) and, in the same statement,
  updates the batch row -- which also takes a row lock that serializes
  every mutating transaction for that batch.
* Every deadline is read from the database clock (``now()``). Lease
  length is the only value the application supplies.
* The CHECK constraint ``total = available + reserved + confirmed`` on
  the batch table means a transaction that would break conservation
  cannot commit.
* Transactions that lose an unavoidable row-lock race receive
  SQLSTATE 40P01 and are retried from the beginning.

The ``*_tx`` functions contain all business logic and take an open
connection/transaction; the public wrappers supply a pooled transaction
with deadlock retry. Keeping the transactional units importable lets the
test suite pin ``expires_at`` against the same ``now()`` the settlement
uses, which is the only way to observe the equality boundary
deterministically.
"""
from __future__ import annotations

import time
from typing import Any

import psycopg

from .errors import (
    BatchNotFound,
    InsufficientQuota,
    InvalidAmount,
    InvalidLeaseDuration,
    InvalidTotal,
    ReservationAlreadyConfirmed,
    ReservationCancelled,
    ReservationExpired,
    ReservationNotFound,
)

MIN_LEASE_SECONDS = 5
MAX_LEASE_SECONDS = 300

_DEADLOCK_RETRIES = 8


# ---------------------------------------------------------------------------
# Low level helpers
# ---------------------------------------------------------------------------

def settle_expired_and_lock(conn: Any, batch_id: int) -> dict:
    """Settle due leases and return current batch counters.

    Lock order is always **batch row first, reservation rows after**, so
    every transaction (reserve/confirm/cancel/settling-read) acquires
    locks in one global order and a reservation/batch lock cycle is
    impossible:

    1. Lock the batch row ``FOR UPDATE`` -- this serializes all mutators
       of one batch.
    2. Flip ``held`` leases with ``expires_at <= now()`` to ``expired``
       (equality counts as expired) and release their millilitre quota.
       The deadline comes exclusively from the database clock.
    3. Apply the released total to the batch counters and return the row.

    Raises ``BatchNotFound`` if the batch does not exist.
    """
    batch_row = conn.execute(
        "SELECT id FROM batch WHERE id = %s FOR UPDATE",
        (batch_id,),
    ).fetchone()
    if batch_row is None:
        raise BatchNotFound(f"batch {batch_id} does not exist")

    expired = conn.execute(
        """
        UPDATE reservation
           SET status = 'expired'
         WHERE batch_id = %(batch_id)s
           AND status = 'held'
           AND expires_at <= now()
        RETURNING amount_ml
        """,
        {"batch_id": batch_id},
    ).fetchall()
    released = sum(r["amount_ml"] for r in expired)

    row = conn.execute(
        """
        UPDATE batch
           SET available_ml = available_ml + %(released)s,
               reserved_ml  = reserved_ml  - %(released)s
         WHERE id = %(batch_id)s
        RETURNING id, total_ml, available_ml, reserved_ml,
                  confirmed_ml, created_at
        """,
        {"released": released, "batch_id": batch_id},
    ).fetchone()
    return dict(row)


def _batch_view(row: dict) -> dict:
    return {
        "id": row["id"],
        "total_ml": row["total_ml"],
        "available_ml": row["available_ml"],
        "reserved_ml": row["reserved_ml"],
        "confirmed_ml": row["confirmed_ml"],
        "created_at": row["created_at"],
    }


_BATCH_COLS = (
    "SELECT id, total_ml, available_ml, reserved_ml, "
    "confirmed_ml, created_at FROM batch WHERE id = %s"
)

_RES_COLS = (
    "fence_token, batch_id, amount_ml, status, expires_at, "
    "created_at, confirmed_at, cancelled_at"
)


def _load_batch(conn, batch_id: int) -> dict:
    return dict(conn.execute(_BATCH_COLS, (batch_id,)).fetchone())


def _reservation_view(row: dict, *, batch: dict | None = None) -> dict:
    view = {
        "fence_token": row["fence_token"],
        "batch_id": row["batch_id"],
        "amount_ml": row["amount_ml"],
        "status": row["status"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "confirmed_at": row["confirmed_at"],
        "cancelled_at": row["cancelled_at"],
    }
    if batch is not None:
        view["batch"] = _batch_view(batch)
    return view


def _validate_amount(amount_ml) -> None:
    if not isinstance(amount_ml, int) or isinstance(amount_ml, bool):
        raise InvalidAmount("amount_ml must be an integer")
    if amount_ml <= 0:
        raise InvalidAmount("amount_ml must be a positive integer",
                            details={"amount_ml": amount_ml})


def _validate_lease(lease_seconds) -> None:
    if (not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)):
        raise InvalidLeaseDuration("lease_seconds must be an integer")
    if not (MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS):
        raise InvalidLeaseDuration(
            f"lease_seconds must be between {MIN_LEASE_SECONDS} and "
            f"{MAX_LEASE_SECONDS}",
            details={"lease_seconds": lease_seconds,
                     "min": MIN_LEASE_SECONDS,
                     "max": MAX_LEASE_SECONDS},
        )


def run_with_retry(pool, fn) -> Any:
    """Run ``fn(conn)`` in a fresh transaction, retrying deadlock victims."""
    last_error: Exception | None = None
    for attempt in range(_DEADLOCK_RETRIES):
        with pool.connection() as conn:
            try:
                with conn.transaction():
                    return fn(conn)
            except psycopg.errors.DeadlockDetected as exc:  # pragma: no cover
                last_error = exc
                time.sleep(0.01 * (2 ** attempt))
                continue
    assert last_error is not None
    raise last_error


def _require_reservation_owner(conn, fence_token: int) -> int:
    owner = conn.execute(
        "SELECT batch_id FROM reservation WHERE fence_token = %s",
        (fence_token,),
    ).fetchone()
    if owner is None:
        raise ReservationNotFound(
            f"reservation {fence_token} does not exist",
            details={"fence_token": fence_token},
        )
    return owner["batch_id"]


def _lock_reservation(conn, fence_token: int) -> dict:
    row = conn.execute(
        f"SELECT {_RES_COLS} FROM reservation "
        "WHERE fence_token = %s FOR UPDATE",
        (fence_token,),
    ).fetchone()
    # The caller already proved ownership earlier in this transaction.
    assert row is not None
    return dict(row)


# ---------------------------------------------------------------------------
# Transactional units
# ---------------------------------------------------------------------------

def create_batch_tx(conn, total_ml: int) -> dict:
    if not isinstance(total_ml, int) or isinstance(total_ml, bool):
        raise InvalidTotal("total_ml must be an integer")
    if total_ml <= 0:
        raise InvalidTotal("total_ml must be a positive integer",
                           details={"total_ml": total_ml})
    row = conn.execute(
        """
        INSERT INTO batch (total_ml, available_ml)
        VALUES (%(total)s, %(total)s)
        RETURNING id, total_ml, available_ml, reserved_ml,
                  confirmed_ml, created_at
        """,
        {"total": total_ml},
    ).fetchone()
    return _batch_view(dict(row))


def reserve_quota_tx(conn, batch_id: int, amount_ml: int,
                     lease_seconds: int) -> dict:
    _validate_amount(amount_ml)
    _validate_lease(lease_seconds)

    batch = settle_expired_and_lock(conn, batch_id)
    if batch["available_ml"] < amount_ml:
        raise InsufficientQuota(
            "not enough available quota for this reservation",
            details={
                "batch_id": batch_id,
                "requested_ml": amount_ml,
                "available_ml": batch["available_ml"],
            },
        )
    # The fence token is an IDENTITY column: strictly increasing and
    # never reused, even after cancellation/expiry. expires_at comes
    # from the database clock only.
    res = conn.execute(
        f"""
        INSERT INTO reservation (batch_id, amount_ml, status, expires_at)
        VALUES (%(batch_id)s, %(amount)s, 'held',
                now() + make_interval(secs => %(lease)s))
        RETURNING {_RES_COLS}
        """,
        {"batch_id": batch_id, "amount": amount_ml,
         "lease": lease_seconds},
    ).fetchone()
    conn.execute(
        """
        UPDATE batch
           SET available_ml = available_ml - %(amount)s,
               reserved_ml  = reserved_ml  + %(amount)s
         WHERE id = %(batch_id)s
        """,
        {"amount": amount_ml, "batch_id": batch_id},
    )
    return _reservation_view(dict(res), batch=_load_batch(conn, batch_id))


def confirm_reservation_tx(conn, fence_token: int) -> dict:
    batch_id = _require_reservation_owner(conn, fence_token)
    # State changes always settle due leases and lock the batch first.
    batch = settle_expired_and_lock(conn, batch_id)
    res = _lock_reservation(conn, fence_token)

    if res["status"] == "confirmed":
        # Idempotent replay: return exactly the original outcome.
        return _reservation_view(res, batch=batch)
    if res["status"] == "expired":
        raise ReservationExpired(
            "reservation has expired and can never be confirmed",
            details={"fence_token": fence_token,
                     "expires_at": res["expires_at"]},
        )
    if res["status"] == "cancelled":
        raise ReservationCancelled(
            "reservation was cancelled and can never be confirmed",
            details={"fence_token": fence_token,
                     "cancelled_at": res["cancelled_at"]},
        )

    # status == 'held': settlement above guarantees expires_at > now(),
    # i.e. the current time is strictly earlier than expires_at.
    updated_res = dict(conn.execute(
        f"""
        UPDATE reservation
           SET status = 'confirmed', confirmed_at = now()
         WHERE fence_token = %s
        RETURNING {_RES_COLS}
        """,
        (fence_token,),
    ).fetchone())
    conn.execute(
        """
        UPDATE batch
           SET reserved_ml  = reserved_ml  - %(amount)s,
               confirmed_ml = confirmed_ml + %(amount)s
         WHERE id = %(batch_id)s
        """,
        {"amount": res["amount_ml"], "batch_id": batch_id},
    )
    return _reservation_view(updated_res,
                             batch=_load_batch(conn, batch_id))


def cancel_reservation_tx(conn, fence_token: int) -> dict:
    batch_id = _require_reservation_owner(conn, fence_token)
    batch = settle_expired_and_lock(conn, batch_id)
    res = _lock_reservation(conn, fence_token)

    if res["status"] == "cancelled":
        # Idempotent cancellation replay.
        return _reservation_view(res, batch=batch)
    if res["status"] == "expired":
        raise ReservationExpired(
            "reservation has already expired",
            details={"fence_token": fence_token,
                     "expires_at": res["expires_at"]},
        )
    if res["status"] == "confirmed":
        raise ReservationAlreadyConfirmed(
            "reservation was already confirmed and cannot be cancelled",
            details={"fence_token": fence_token,
                     "confirmed_at": res["confirmed_at"]},
        )

    updated_res = dict(conn.execute(
        f"""
        UPDATE reservation
           SET status = 'cancelled', cancelled_at = now()
         WHERE fence_token = %s
        RETURNING {_RES_COLS}
        """,
        (fence_token,),
    ).fetchone())
    conn.execute(
        """
        UPDATE batch
           SET available_ml = available_ml + %(amount)s,
               reserved_ml  = reserved_ml  - %(amount)s
         WHERE id = %(batch_id)s
        """,
        {"amount": res["amount_ml"], "batch_id": batch_id},
    )
    return _reservation_view(updated_res,
                             batch=_load_batch(conn, batch_id))


def get_batch_tx(conn, batch_id: int) -> dict:
    # Reads settle due leases too: reported reserved_ml always counts only
    # valid leases according to the database clock, and conservation holds.
    return _batch_view(settle_expired_and_lock(conn, batch_id))


def get_reservation_tx(conn, fence_token: int) -> dict:
    batch_id = _require_reservation_owner(conn, fence_token)
    batch = settle_expired_and_lock(conn, batch_id)
    row = conn.execute(
        f"SELECT {_RES_COLS} FROM reservation WHERE fence_token = %s",
        (fence_token,),
    ).fetchone()
    return _reservation_view(dict(row), batch=_batch_view(batch))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_batch(pool, total_ml: int) -> dict:
    return run_with_retry(pool, lambda c: create_batch_tx(c, total_ml))


def reserve_quota(pool, batch_id: int, amount_ml: int,
                  lease_seconds: int) -> dict:
    return run_with_retry(
        pool,
        lambda c: reserve_quota_tx(c, batch_id, amount_ml, lease_seconds),
    )


def confirm_reservation(pool, fence_token: int) -> dict:
    return run_with_retry(pool,
                          lambda c: confirm_reservation_tx(c, fence_token))


def cancel_reservation(pool, fence_token: int) -> dict:
    return run_with_retry(pool,
                          lambda c: cancel_reservation_tx(c, fence_token))


def get_batch(pool, batch_id: int) -> dict:
    return run_with_retry(pool, lambda c: get_batch_tx(c, batch_id))


def get_reservation(pool, fence_token: int) -> dict:
    return run_with_retry(pool, lambda c: get_reservation_tx(c, fence_token))
