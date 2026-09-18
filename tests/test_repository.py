"""Reservation lifecycle and conservation invariants against real PostgreSQL."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app import repository
from app.errors import ErrorCode, ServiceError

from .conftest import assert_conserved, fetch_batch, freeze_at


def make_batch(pool, total: int = 1000) -> int:
    with pool.connection() as conn:
        return repository.create_batch(conn, total)["id"]


def test_batch_initial_balance_conserves(pool):
    bid = make_batch(pool, 500)
    b = assert_conserved(pool, bid)
    assert b["total_ml"] == 500
    assert (b["available_ml"], b["reserved_ml"], b["confirmed_ml"]) == (500, 0, 0)


def test_total_ml_is_immutable(pool):
    bid = make_batch(pool, 500)
    with pool.connection() as conn:
        with pytest.raises(Exception) as ei:
            conn.execute("UPDATE batches SET total_ml = 999 WHERE id = %s", [bid])
        conn.rollback()
    assert fetch_batch(pool, bid)["total_ml"] == 500
    # The accounting check is actually enforced server-side, not app-side.
    assert "immutable" in str(ei.value).lower()


def test_reserve_confirm_flow(pool):
    bid = make_batch(pool)
    with pool.connection() as conn:
        r = repository.reserve(conn, bid, 300, 60)
        token = r["token"]
        assert r["status"] == "held"
        assert (r["expires_at"] - r["created_at"]) == timedelta(seconds=60)
    b = assert_conserved(pool, bid)
    assert (b["available_ml"], b["reserved_ml"], b["confirmed_ml"]) == (700, 300, 0)

    with pool.connection() as conn:
        out = repository.confirm(conn, bid, token)
        assert out["status"] == "confirmed"
        assert out["token"] == token
    b = assert_conserved(pool, bid)
    assert (b["available_ml"], b["reserved_ml"], b["confirmed_ml"]) == (700, 0, 300)

    # Replay returns the exact original outcome, balances untouched.
    with pool.connection() as conn:
        again = repository.confirm(conn, bid, token)
        assert again["status"] == "confirmed"
        assert again["confirmed_at"] == out["confirmed_at"]
    b = assert_conserved(pool, bid)
    assert b["confirmed_ml"] == 300


def test_reserve_cancel_flow(pool):
    bid = make_batch(pool)
    with pool.connection() as conn:
        token = repository.reserve(conn, bid, 250, 60)["token"]
    with pool.connection() as conn:
        out = repository.cancel(conn, bid, token)
        assert out["status"] == "cancelled"
    b = assert_conserved(pool, bid)
    assert (b["available_ml"], b["reserved_ml"], b["confirmed_ml"]) == (1000, 0, 0)

    # A cancelled token is permanently barred from confirmation.
    with pool.connection() as conn:
        with pytest.raises(ServiceError) as ei:
            repository.confirm(conn, bid, token)
    assert ei.value.code is ErrorCode.RESERVATION_CANCELLED
    assert_conserved(pool, bid)

    # Cancel replay is idempotent and never double-refunds.
    with pool.connection() as conn:
        again = repository.cancel(conn, bid, token)
        assert again["status"] == "cancelled"
    b = assert_conserved(pool, bid)
    assert b["available_ml"] == 1000


def test_confirmed_cannot_be_cancelled(pool):
    bid = make_batch(pool)
    with pool.connection() as conn:
        token = repository.reserve(conn, bid, 100, 60)["token"]
        repository.confirm(conn, bid, token)
        with pytest.raises(ServiceError) as ei:
            repository.cancel(conn, bid, token)
    assert ei.value.code is ErrorCode.RESERVATION_CONFIRMED
    assert_conserved(pool, bid)


def test_insufficient_balance(pool):
    bid = make_batch(pool, 100)
    with pool.connection() as conn:
        repository.reserve(conn, bid, 80, 60)
        with pytest.raises(ServiceError) as ei:
            repository.reserve(conn, bid, 21, 60)
    assert ei.value.code is ErrorCode.INSUFFICIENT_BALANCE
    b = assert_conserved(pool, bid)
    assert b["available_ml"] == 20

    # Exactly the remaining amount must still succeed.
    with pool.connection() as conn:
        repository.reserve(conn, bid, 20, 60)
    b = assert_conserved(pool, bid)
    assert b["available_ml"] == 0


@pytest.mark.parametrize("amount", [0, -5])
def test_reserve_amount_must_be_positive(pool, amount):
    bid = make_batch(pool)
    with pool.connection() as conn:
        with pytest.raises(ServiceError) as ei:
            repository.reserve(conn, bid, amount, 60)
    assert ei.value.code is ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize("lease", [4, 0, -1, 301, 1000])
def test_lease_bounds(pool, lease):
    bid = make_batch(pool)
    with pool.connection() as conn:
        with pytest.raises(ServiceError) as ei:
            repository.reserve(conn, bid, 10, lease)
    assert ei.value.code is ErrorCode.VALIDATION_ERROR


def test_lease_boundary_values_accepted(pool):
    bid = make_batch(pool, 10_000)
    with pool.connection() as conn:
        t1 = repository.reserve(conn, bid, 10, 5)["token"]
        t2 = repository.reserve(conn, bid, 10, 300)["token"]
        assert repository.confirm(conn, bid, t1)["status"] == "confirmed"
        assert repository.confirm(conn, bid, t2)["status"] == "confirmed"
    assert_conserved(pool, bid)


def test_fence_tokens_strictly_increasing_and_never_reused(pool):
    bid = make_batch(pool, 10_000)
    tokens = []
    for _ in range(20):
        with pool.connection() as conn:
            r = repository.reserve(conn, bid, 5, 300)
            tokens.append(r["token"])
            # terminate it so quota frees and ids keep moving
            repository.cancel(conn, bid, r["token"])
    assert tokens == sorted(tokens)
    assert len(set(tokens)) == len(tokens)
    # strictly increasing
    assert all(b > a for a, b in zip(tokens, tokens[1:]))


def test_tokens_increase_across_terminal_states(pool):
    bid = make_batch(pool, 10_000)
    with pool.connection() as conn:
        t1 = repository.reserve(conn, bid, 1, 5)["token"]
    # expire t1 by settling past its deadline
    with pool.connection() as conn:
        freeze_at(conn, repository._db_now(conn) + timedelta(seconds=6))
        repository.get_batch(conn, bid)
    with pool.connection() as conn:
        t2 = repository.reserve(conn, bid, 1, 5)["token"]
    assert t2 > t1
    assert_conserved(pool, bid)


def test_expired_reservation_releases_balance_and_rejects_confirm(pool):
    bid = make_batch(pool, 100)
    with pool.connection() as conn:
        token = repository.reserve(conn, bid, 40, 5)["token"]
    # Settle via an unrelated later operation with a frozen DB clock past expiry.
    with pool.connection() as conn:
        freeze_at(conn, repository._db_now(conn) + timedelta(seconds=6))
        with pytest.raises(ServiceError) as ei:
            repository.confirm(conn, bid, token)
    assert ei.value.code is ErrorCode.RESERVATION_EXPIRED
    b = assert_conserved(pool, bid)
    assert b["available_ml"] == 100
    assert b["reserved_ml"] == 0


def test_unknown_entities(pool):
    with pool.connection() as conn:
        with pytest.raises(ServiceError) as ei:
            repository.get_batch(conn, 9999)
    assert ei.value.code is ErrorCode.BATCH_NOT_FOUND

    bid = make_batch(pool)
    with pool.connection() as conn:
        with pytest.raises(ServiceError) as ei:
            repository.confirm(conn, bid, 9999)
    assert ei.value.code is ErrorCode.RESERVATION_NOT_FOUND
