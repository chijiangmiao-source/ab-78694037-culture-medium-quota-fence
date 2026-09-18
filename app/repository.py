"""Transactional core.

Every state-changing (and balance-reading) operation follows the same protocol
inside a single database transaction:

    1. SELECT ... FOR UPDATE on the batch row  (serializes all mutators)
    2. read the database clock (clock_timestamp())
    3. settle held reservations whose expires_at <= now  (equality == expired)
    4. perform the operation, re-checking every precondition against locked rows

The reservation id is a GENERATED ALWAYS AS IDENTITY sequence: it is the fence
token -- strictly increasing within a batch (in fact globally) and never reused.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from psycopg import Connection

from .errors import ErrorCode, ServiceError

LEASE_MIN_SECONDS = 5
LEASE_MAX_SECONDS = 300


def _db_now(conn: Connection):
    """Database clock.

    By default this is the real transaction timestamp now(). The optional
    transaction-local GUC ``app.frozen_now`` only exists to make boundary tests
    deterministic (it is parsed/cast by PostgreSQL and never set by the API);
    production code paths always take the real now() branch.
    """
    frozen = conn.execute("SELECT current_setting('app.frozen_now', true) AS v").fetchone()["v"]
    if frozen:
        return conn.execute("SELECT %s::timestamptz AS v", [frozen]).fetchone()["v"]
    # Real current instant (not transaction_timestamp(), which is frozen at the
    # start of the transaction and could be stale after waiting on a row lock).
    return conn.execute("SELECT clock_timestamp() AS v").fetchone()["v"]


def _lock_and_settle(conn: Connection, batch_id: int) -> tuple[Any, dict[str, Any]]:
    """Lock the batch, expire due reservations with the DB clock, return (now, batch).

    The batch row is locked BEFORE reading the clock, so a transaction that
    waited behind another mutator never decides with a timestamp captured while
    it was queued.
    """
    batch = conn.execute(
        """
        SELECT id, total_ml, available_ml, reserved_ml, confirmed_ml, created_at
          FROM batches
         WHERE id = %s
         FOR UPDATE
        """,
        [batch_id],
    ).fetchone()
    if batch is None:
        raise ServiceError(404, ErrorCode.BATCH_NOT_FOUND, f"batch {batch_id} not found")
    batch = dict(batch)

    now = _db_now(conn)

    # Equality expires: expires_at == now is already expired (strict < required).
    expired = conn.execute(
        """
        UPDATE reservations
           SET status = 'expired',
               settled_at = %s
         WHERE batch_id = %s
           AND status = 'held'
           AND expires_at <= %s
         RETURNING amount_ml
        """,
        [now, batch_id, now],
    ).fetchall()
    if expired:
        released = sum(row["amount_ml"] for row in expired)
        conn.execute(
            """
            UPDATE batches
               SET available_ml = available_ml + %s,
                   reserved_ml  = reserved_ml  - %s
             WHERE id = %s
            """,
            [released, released, batch_id],
        )
        batch["available_ml"] += released
        batch["reserved_ml"] -= released

    return now, batch


def _lock_reservation(conn: Connection, batch_id: int, token: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, batch_id, amount_ml, status, expires_at,
               created_at, confirmed_at, cancelled_at, settled_at
          FROM reservations
         WHERE id = %s AND batch_id = %s
         FOR UPDATE
        """,
        [token, batch_id],
    ).fetchone()
    if row is None:
        raise ServiceError(
            404,
            ErrorCode.RESERVATION_NOT_FOUND,
            f"reservation token {token} not found in batch {batch_id}",
            details={"batch_id": batch_id, "token": token},
        )
    return dict(row)


def create_batch(conn: Connection, total_ml: int) -> dict[str, Any]:
    row = conn.execute(
        """
        INSERT INTO batches (total_ml, available_ml, reserved_ml, confirmed_ml)
        VALUES (%s, %s, 0, 0)
        RETURNING id, total_ml, available_ml, reserved_ml, confirmed_ml, created_at
        """,
        [total_ml, total_ml],
    ).fetchone()
    return dict(row)


def get_batch(conn: Connection, batch_id: int) -> dict[str, Any]:
    # Reading balances also settles, so any query observes consistent, current
    # buckets (and the conservation invariant is directly observable).
    _, batch = _lock_and_settle(conn, batch_id)
    return batch


def list_batches(conn: Connection) -> list[dict[str, Any]]:
    # Settle every batch (in id order, which keeps locking deadlock-free) so the
    # list view reports the same post-settlement balances as GET /batches/{id}.
    ids = [r["id"] for r in conn.execute("SELECT id FROM batches ORDER BY id").fetchall()]
    for bid in ids:
        _lock_and_settle(conn, bid)
    rows = conn.execute(
        """
        SELECT id, total_ml, available_ml, reserved_ml, confirmed_ml, created_at
          FROM batches
         ORDER BY id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _strict_positive_int(name: str, value: Any) -> None:
    # bool is a subclass of int but is not an integer millilitre amount.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ServiceError(
            422,
            ErrorCode.VALIDATION_ERROR,
            f"{name} must be a positive integer",
            details={"field": name, "value": value},
        )


def reserve(conn: Connection, batch_id: int, amount_ml: int, lease_seconds: int) -> dict[str, Any]:
    _strict_positive_int("amount_ml", amount_ml)
    if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not (
        LEASE_MIN_SECONDS <= lease_seconds <= LEASE_MAX_SECONDS
    ):
        raise ServiceError(
            422,
            ErrorCode.VALIDATION_ERROR,
            f"lease_seconds must be an integer between {LEASE_MIN_SECONDS} and {LEASE_MAX_SECONDS}",
            details={"field": "lease_seconds", "value": lease_seconds},
        )

    now, batch = _lock_and_settle(conn, batch_id)

    if amount_ml > batch["available_ml"]:
        raise ServiceError(
            409,
            ErrorCode.INSUFFICIENT_BALANCE,
            (
                f"insufficient available balance: requested {amount_ml} ml, "
                f"available {batch['available_ml']} ml"
            ),
            details={
                "batch_id": batch_id,
                "requested_ml": amount_ml,
                "available_ml": batch["available_ml"],
            },
        )

    expires_at = now + timedelta(seconds=lease_seconds)
    token = conn.execute(
        """
        INSERT INTO reservations (batch_id, amount_ml, status, expires_at, created_at)
        VALUES (%s, %s, 'held', %s, %s)
        RETURNING id
        """,
        [batch_id, amount_ml, expires_at, now],
    ).fetchone()["id"]

    conn.execute(
        """
        UPDATE batches
           SET available_ml = available_ml - %s,
               reserved_ml  = reserved_ml  + %s
         WHERE id = %s
        """,
        [amount_ml, amount_ml, batch_id],
    )

    return {
        "batch_id": batch_id,
        "token": token,
        "amount_ml": amount_ml,
        "status": "held",
        "created_at": now,
        "expires_at": expires_at,
    }


def confirm(conn: Connection, batch_id: int, token: int) -> dict[str, Any]:
    now, _ = _lock_and_settle(conn, batch_id)
    reservation = _lock_reservation(conn, batch_id, token)

    if reservation["status"] == "confirmed":
        # Idempotent replay: return the original outcome unchanged.
        return _reservation_outcome(reservation)

    if reservation["status"] == "cancelled":
        raise ServiceError(
            409,
            ErrorCode.RESERVATION_CANCELLED,
            f"reservation token {token} was cancelled and can never be confirmed",
            details={"batch_id": batch_id, "token": token, "status": "cancelled"},
        )

    if reservation["status"] == "expired" or now >= reservation["expires_at"]:
        # Settlement normally already flipped it; the clock check is defensive
        # and makes the strict-equality rule explicit.
        raise ServiceError(
            409,
            ErrorCode.RESERVATION_EXPIRED,
            f"reservation token {token} has expired at {reservation['expires_at'].isoformat()}",
            details={
                "batch_id": batch_id,
                "token": token,
                "status": "expired",
                "expires_at": reservation["expires_at"].isoformat(),
                "now": now.isoformat(),
            },
        )

    # held and strictly before expiry -> confirm exactly once.
    conn.execute(
        """
        UPDATE reservations
           SET status = 'confirmed',
               confirmed_at = %s
         WHERE id = %s AND batch_id = %s AND status = 'held'
        """,
        [now, token, batch_id],
    )
    conn.execute(
        """
        UPDATE batches
           SET reserved_ml  = reserved_ml  - %s,
               confirmed_ml = confirmed_ml + %s
         WHERE id = %s
        """,
        [reservation["amount_ml"], reservation["amount_ml"], batch_id],
    )
    reservation["status"] = "confirmed"
    reservation["confirmed_at"] = now
    return _reservation_outcome(reservation)


def cancel(conn: Connection, batch_id: int, token: int) -> dict[str, Any]:
    now, _ = _lock_and_settle(conn, batch_id)
    reservation = _lock_reservation(conn, batch_id, token)

    if reservation["status"] == "cancelled":
        return _reservation_outcome(reservation)

    if reservation["status"] == "confirmed":
        raise ServiceError(
            409,
            ErrorCode.RESERVATION_CONFIRMED,
            f"reservation token {token} is already confirmed and cannot be cancelled",
            details={"batch_id": batch_id, "token": token, "status": "confirmed"},
        )

    if reservation["status"] == "expired" or now >= reservation["expires_at"]:
        raise ServiceError(
            409,
            ErrorCode.RESERVATION_EXPIRED,
            f"reservation token {token} has expired and cannot be cancelled",
            details={"batch_id": batch_id, "token": token, "status": "expired"},
        )

    conn.execute(
        """
        UPDATE reservations
           SET status = 'cancelled',
               cancelled_at = %s
         WHERE id = %s AND batch_id = %s AND status = 'held'
        """,
        [now, token, batch_id],
    )
    conn.execute(
        """
        UPDATE batches
           SET available_ml = available_ml + %s,
               reserved_ml  = reserved_ml  - %s
         WHERE id = %s
        """,
        [reservation["amount_ml"], reservation["amount_ml"], batch_id],
    )
    reservation["status"] = "cancelled"
    reservation["cancelled_at"] = now
    return _reservation_outcome(reservation)


def get_reservation(conn: Connection, batch_id: int, token: int) -> dict[str, Any]:
    _lock_and_settle(conn, batch_id)
    return _reservation_outcome(_lock_reservation(conn, batch_id, token))


def _reservation_outcome(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch_id": r["batch_id"],
        "token": r["id"],
        "amount_ml": r["amount_ml"],
        "status": r["status"],
        "created_at": r["created_at"],
        "expires_at": r["expires_at"],
        "confirmed_at": r.get("confirmed_at"),
        "cancelled_at": r.get("cancelled_at"),
        "settled_at": r.get("settled_at"),
    }
