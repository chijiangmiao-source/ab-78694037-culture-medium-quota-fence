"""Real contention tests against PostgreSQL (threads = independent transactions)."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from app import repository
from app.errors import ErrorCode, ServiceError

from .conftest import assert_conserved, fetch_batch, freeze_at


class Barrier:
    def __init__(self, n):
        self.b = threading.Barrier(n)

    def wait(self):
        self.b.wait()


def test_concurrent_respects_available_balance(pool):
    total = 2000
    ask = 100
    n_workers = 50
    with pool.connection() as conn:
        bid = repository.create_batch(conn, total)["id"]

    barrier = threading.Barrier(n_workers)

    def worker(_):
        with pool.connection() as conn:
            barrier.wait()
            try:
                r = repository.reserve(conn, bid, ask, 300)
                return ("ok", r["token"])
            except ServiceError as e:
                return (e.code.value, None)

    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        results = list(ex.map(worker, range(n_workers)))

    ok = [r for r in results if r[0] == "ok"]
    short = [r for r in results if r[0] == ErrorCode.INSUFFICIENT_BALANCE.value]
    assert len(ok) == total // ask
    assert len(ok) + len(short) == n_workers
    tokens = {t for _, t in ok}
    assert len(tokens) == len(ok)  # unique fence tokens

    b = assert_conserved(pool, bid)
    assert b["available_ml"] == total - len(ok) * ask
    assert b["reserved_ml"] == len(ok) * ask
    assert b["confirmed_ml"] == 0


def test_concurrent_confirm_same_token_confirms_once(pool):
    n_workers = 20
    with pool.connection() as conn:
        bid = repository.create_batch(conn, 10_000)["id"]
        token = repository.reserve(conn, bid, 500, 300)["token"]

    barrier = threading.Barrier(n_workers)

    def worker(_):
        with pool.connection() as conn:
            barrier.wait()
            try:
                out = repository.confirm(conn, bid, token)
                return ("ok", out["status"], out["confirmed_at"])
            except ServiceError as e:
                return ("err", e.code.value, None)

    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        results = list(ex.map(worker, range(n_workers)))

    assert {r[1] for r in results} == {"confirmed"}
    confirmed_ats = {r[2] for r in results}
    assert len(confirmed_ats) == 1  # every replay returns the original result

    b = assert_conserved(pool, bid)
    assert b["confirmed_ml"] == 500
    assert b["reserved_ml"] == 0
    assert b["available_ml"] == 9500


def test_concurrent_confirm_vs_cancel_single_outcome(pool):
    n_workers = 24
    with pool.connection() as conn:
        bid = repository.create_batch(conn, 10_000)["id"]
        token = repository.reserve(conn, bid, 700, 300)["token"]

    barrier = threading.Barrier(n_workers)

    def worker(i):
        action = "confirm" if i % 2 == 0 else "cancel"
        with pool.connection() as conn:
            barrier.wait()
            try:
                out = (
                    repository.confirm(conn, bid, token)
                    if action == "confirm"
                    else repository.cancel(conn, bid, token)
                )
                return (action, "ok", out["status"])
            except ServiceError as e:
                return (action, "err", e.code.value)

    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        results = list(ex.map(worker, range(n_workers)))

    b = assert_conserved(pool, bid)
    final = fetch_batch(pool, bid)

    # Exactly one terminal outcome; money is either confirmed once or refunded once.
    statuses = {r[2] for r in results if r[1] == "ok"}
    assert len(statuses) == 1
    winner = next(iter(statuses))
    assert winner in {"confirmed", "cancelled"}

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT status FROM reservations WHERE id = %s", [token]
        ).fetchone()
        assert row["status"] == winner

    if winner == "confirmed":
        assert b["confirmed_ml"] == 700
        assert b["available_ml"] == 9300
        # cancels arriving after the confirm must be rejected, never refund
        assert all(
            r[2] in {"confirmed", ErrorCode.RESERVATION_CONFIRMED.value}
            for r in results
        )
    else:
        assert b["confirmed_ml"] == 0
        assert b["available_ml"] == 10_000
        # confirms arriving after the cancel must be rejected, never double-spend
        assert all(
            r[2] in {"cancelled", ErrorCode.RESERVATION_CANCELLED.value}
            for r in results
        )


def test_concurrent_expired_holds_reject_then_balance_reusable(pool):
    # Many devices grab the whole quota with short leases; after expiry a storm
    # of late confirms must all fail, and the freed quota must be reusable once.
    with pool.connection() as conn:
        bid = repository.create_batch(conn, 1000)["id"]
        t0 = repository._db_now(conn)
        freeze_at(conn, t0)
        t1 = repository.reserve(conn, bid, 600, 5)["token"]
        t2 = repository.reserve(conn, bid, 400, 5)["token"]

    barrier = threading.Barrier(2)

    def late_confirm(token):
        with pool.connection() as conn:
            freeze_at(conn, t0 + timedelta(seconds=6))
            barrier.wait()
            try:
                repository.confirm(conn, bid, token)
                return "ok"
            except ServiceError as e:
                return e.code

    with ThreadPoolExecutor(max_workers=2) as ex:
        outcomes = list(ex.map(late_confirm, [t1, t2]))
    assert outcomes == [ErrorCode.RESERVATION_EXPIRED, ErrorCode.RESERVATION_EXPIRED]

    b = assert_conserved(pool, bid)
    assert (b["available_ml"], b["reserved_ml"], b["confirmed_ml"]) == (1000, 0, 0)

    # Expired quota is fully reusable.
    with pool.connection() as conn:
        freeze_at(conn, t0 + timedelta(seconds=10))
        r = repository.reserve(conn, bid, 1000, 300)
        out = repository.confirm(conn, bid, r["token"])
        assert out["status"] == "confirmed"
    b = assert_conserved(pool, bid)
    assert b["confirmed_ml"] == 1000


def test_mixed_concurrent_lifecycle_never_overcommits(pool):
    # Random-ish interleaving of reserve/confirm/cancel over a shared batch:
    # conservation must hold at the end and confirmed never exceeds total.
    with pool.connection() as conn:
        bid = repository.create_batch(conn, 5_000)["id"]

    tokens = []
    lock = threading.Lock()

    def reserve_worker(_):
        with pool.connection() as conn:
            try:
                r = repository.reserve(conn, bid, 250, 300)
                with lock:
                    tokens.append(r["token"])
                return "reserved"
            except ServiceError as e:
                return e.code.value

    with ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(reserve_worker, range(40)))

    # Fire one confirm and one cancel per token simultaneously. The barrier
    # size equals the number of jobs, so the pool must be at least that large.
    def lifecycle(token, do_confirm: bool):
        with pool.connection() as conn:
            barrier.wait()
            try:
                if do_confirm:
                    repository.confirm(conn, bid, token)
                else:
                    repository.cancel(conn, bid, token)
                return "ok"
            except ServiceError as e:
                return e.code.value

    jobs = [(t, True) for t in tokens] + [(t, False) for t in tokens]
    barrier = threading.Barrier(len(jobs))
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        list(ex.map(lambda j: lifecycle(*j), jobs))

    b = assert_conserved(pool, bid)
    # Every held token reached exactly one terminal state; nothing stays held.
    assert b["reserved_ml"] == 0
    assert b["confirmed_ml"] % 250 == 0
    assert b["confirmed_ml"] <= 250 * len(tokens)
    assert b["confirmed_ml"] + b["available_ml"] == 5_000
