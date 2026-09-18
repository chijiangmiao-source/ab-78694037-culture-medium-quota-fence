"""Exact-boundary expiry tests.

Confirmation succeeds only when the database instant is STRICTLY earlier than
expires_at; equality means expired. Timing is made deterministic with the
transaction-local frozen clock GUC (cast by PostgreSQL itself).
"""
from __future__ import annotations

import time
from datetime import timedelta

import pytest

from app import repository
from app.errors import ErrorCode, ServiceError

from .conftest import assert_conserved, freeze_at


def test_confirm_succeeds_strictly_before_expiry(pool):
    bid = None
    with pool.connection() as conn:
        t0 = repository._db_now(conn)
        bid = repository.create_batch(conn, 100)["id"]
        freeze_at(conn, t0)
        r = repository.reserve(conn, bid, 40, 5)
        assert r["expires_at"] == t0 + timedelta(seconds=5)

    # one microsecond before the deadline -> still valid
    with pool.connection() as conn:
        t0 = conn.execute("SELECT created_at FROM reservations WHERE id = %s", [r["token"]]).fetchone()["created_at"]
        freeze_at(conn, t0 + timedelta(seconds=5, microseconds=-1))
        out = repository.confirm(conn, bid, r["token"])
        assert out["status"] == "confirmed"
    assert_conserved(pool, bid)


def test_confirm_fails_when_now_equals_expires_at(pool):
    with pool.connection() as conn:
        t0 = repository._db_now(conn)
        bid = repository.create_batch(conn, 100)["id"]
        freeze_at(conn, t0)
        r = repository.reserve(conn, bid, 40, 5)
        deadline = r["expires_at"]
        assert deadline == t0 + timedelta(seconds=5)

    # now == expires_at EXACTLY -> expired (strict inequality required)
    with pool.connection() as conn:
        freeze_at(conn, deadline)
        with pytest.raises(ServiceError) as ei:
            repository.confirm(conn, bid, r["token"])
    assert ei.value.code is ErrorCode.RESERVATION_EXPIRED
    b = assert_conserved(pool, bid)
    assert (b["available_ml"], b["reserved_ml"], b["confirmed_ml"]) == (100, 0, 0)

    # Permanent: even an earlier-looking replay cannot revive it.
    with pool.connection() as conn:
        created = conn.execute(
            "SELECT created_at FROM reservations WHERE id = %s", [r["token"]]
        ).fetchone()["created_at"]
        freeze_at(conn, created + timedelta(seconds=1))
        with pytest.raises(ServiceError) as ei:
            repository.confirm(conn, bid, r["token"])
    assert ei.value.code is ErrorCode.RESERVATION_EXPIRED


def test_cancel_fails_when_now_equals_expires_at(pool):
    with pool.connection() as conn:
        t0 = repository._db_now(conn)
        bid = repository.create_batch(conn, 100)["id"]
        freeze_at(conn, t0)
        r = repository.reserve(conn, bid, 40, 5)

    with pool.connection() as conn:
        freeze_at(conn, r["expires_at"])
        with pytest.raises(ServiceError) as ei:
            repository.cancel(conn, bid, r["token"])
    assert ei.value.code is ErrorCode.RESERVATION_EXPIRED
    assert_conserved(pool, bid)


def test_settlement_uses_database_clock_not_client_time(pool):
    # A reserve created at frozen time T with lease 5 expires at T+5 according
    # to the server; the next operation at real now (much later in frozen
    # terms is irrelevant) settles it with the DB clock.
    with pool.connection() as conn:
        t0 = repository._db_now(conn)
        bid = repository.create_batch(conn, 100)["id"]
        freeze_at(conn, t0 - timedelta(hours=1))
        r = repository.reserve(conn, bid, 40, 5)
    with pool.connection() as conn:
        # Real current instant is well after (t0 - 1h) + 5s, so it is expired.
        b = repository.get_batch(conn, bid)
        assert b["available_ml"] == 100
        assert b["reserved_ml"] == 0
        with pytest.raises(ServiceError) as ei:
            repository.confirm(conn, bid, r["token"])
    assert ei.value.code is ErrorCode.RESERVATION_EXPIRED


def test_real_wall_clock_expiry(pool):
    """End-to-end with the genuine clock and a 5 second lease."""
    with pool.connection() as conn:
        bid = repository.create_batch(conn, 100)["id"]
        r = repository.reserve(conn, bid, 40, 5)
    time.sleep(5.2)
    with pool.connection() as conn:
        with pytest.raises(ServiceError) as ei:
            repository.confirm(conn, bid, r["token"])
    assert ei.value.code is ErrorCode.RESERVATION_EXPIRED
    b = assert_conserved(pool, bid)
    assert b["available_ml"] == 100


def test_real_wall_clock_confirm_within_lease(pool):
    with pool.connection() as conn:
        bid = repository.create_batch(conn, 100)["id"]
        r = repository.reserve(conn, bid, 40, 5)
    time.sleep(4.5)
    with pool.connection() as conn:
        out = repository.confirm(conn, bid, r["token"])
    assert out["status"] == "confirmed"
    assert_conserved(pool, bid)
