"""End-to-end acceptance: the device-gateway contract over real HTTP.

These tests know nothing about the database: they exercise only the public API
and assert that a gateway can ever observe a single effective confirmation, an
unambiguous expiry/cancellation/insufficient-balance result, and that balances
conserve on every query.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok"}


def test_batch_creation_and_conservation(make_batch, get_balance):
    bid = make_batch(750)
    b = get_balance(bid)
    assert b["total_ml"] == 750
    assert (b["available_ml"], b["reserved_ml"], b["confirmed_ml"]) == (750, 0, 0)


def test_reserve_confirm_is_effective_exactly_once(client, make_batch, get_balance):
    bid = make_batch(1000)
    r = client.post(
        f"/batches/{bid}/reservations", json={"amount_ml": 300, "lease_seconds": 300}
    )
    assert r.status_code == 201
    token = r.json()["token"]

    first = client.post(f"/batches/{bid}/reservations/{token}/confirm")
    assert first.status_code == 200
    body1 = first.json()
    assert body1["status"] == "confirmed"

    # Duplicate submissions return the original outcome, never re-deduct.
    second = client.post(f"/batches/{bid}/reservations/{token}/confirm")
    assert second.status_code == 200
    body2 = second.json()
    assert body2["status"] == "confirmed"
    assert body2["confirmed_at"] == body1["confirmed_at"]

    b = get_balance(bid)
    assert b["confirmed_ml"] == 300
    assert b["reserved_ml"] == 0
    assert b["available_ml"] == 700


def test_cancel_is_terminal(client, make_batch, get_balance):
    bid = make_batch(1000)
    token = client.post(
        f"/batches/{bid}/reservations", json={"amount_ml": 250, "lease_seconds": 300}
    ).json()["token"]

    assert client.post(f"/batches/{bid}/reservations/{token}/cancel").status_code == 200
    late = client.post(f"/batches/{bid}/reservations/{token}/confirm")
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "reservation_cancelled"

    b = get_balance(bid)
    assert b == {**b, "available_ml": 1000, "reserved_ml": 0, "confirmed_ml": 0}


def test_insufficient_balance_is_explicit(client, make_batch, get_balance):
    bid = make_batch(100)
    client.post(
        f"/batches/{bid}/reservations", json={"amount_ml": 80, "lease_seconds": 300}
    )
    r = client.post(
        f"/batches/{bid}/reservations", json={"amount_ml": 21, "lease_seconds": 300}
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "insufficient_balance"
    assert err["details"]["available_ml"] == 20
    assert err["details"]["requested_ml"] == 21
    get_balance(bid)


def test_validation_errors_are_structured(client, make_batch):
    bid = make_batch(1000)
    for payload in (
        {"amount_ml": 0, "lease_seconds": 60},
        {"amount_ml": -3, "lease_seconds": 60},
        {"amount_ml": 10, "lease_seconds": 4},
        {"amount_ml": 10, "lease_seconds": 301},
    ):
        r = client.post(f"/batches/{bid}/reservations", json=payload)
        assert r.status_code == 422, payload
        assert r.json()["error"]["code"] == "validation_error"

    r = client.post("/batches", json={"total_ml": 0})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_unknown_entities_structured(client, make_batch):
    r = client.get("/batches/987654321")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "batch_not_found"

    bid = make_batch(10)
    r = client.post(f"/batches/{bid}/reservations/424242/confirm")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "reservation_not_found"


def test_fence_tokens_are_monotonic(client, make_batch):
    bid = make_batch(1_000_000)
    tokens = []
    for _ in range(15):
        r = client.post(
            f"/batches/{bid}/reservations", json={"amount_ml": 1, "lease_seconds": 300}
        )
        tokens.append(r.json()["token"])
    assert all(b > a for a, b in zip(tokens, tokens[1:]))
    assert len(set(tokens)) == len(tokens)


def test_expiry_is_unambiguous_and_quota_reusable(client, make_batch, get_balance):
    bid = make_batch(100)
    token = client.post(
        f"/batches/{bid}/reservations", json={"amount_ml": 100, "lease_seconds": 5}
    ).json()["token"]

    # A query at/after expiry observes the released balance with conservation.
    import time

    time.sleep(5.2)
    get_balance(bid)

    late = client.post(f"/batches/{bid}/reservations/{token}/confirm")
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "reservation_expired"

    # Expired tokens stay dead.
    assert (
        client.post(f"/batches/{bid}/reservations/{token}/confirm").status_code == 409
    )

    b = get_balance(bid)
    assert (b["available_ml"], b["reserved_ml"], b["confirmed_ml"]) == (100, 0, 0)

    # The released quota can be reserved/confirmed exactly once more.
    r2 = client.post(
        f"/batches/{bid}/reservations", json={"amount_ml": 100, "lease_seconds": 300}
    )
    assert r2.status_code == 201
    token2 = r2.json()["token"]
    assert client.post(f"/batches/{bid}/reservations/{token2}/confirm").status_code == 200
    b = get_balance(bid)
    assert b["confirmed_ml"] == 100
    assert client.post(
        f"/batches/{bid}/reservations", json={"amount_ml": 1, "lease_seconds": 300}
    ).status_code == 409


def test_concurrent_reservations_never_overcommit(base_url, make_batch, get_balance):
    bid = make_batch(2000)
    n = 50
    barrier = threading.Barrier(n)
    results = []
    lock = threading.Lock()

    def worker(_):
        barrier.wait()
        with httpx.Client(base_url=base_url, timeout=10) as c:
            r = c.post(
                f"/batches/{bid}/reservations",
                json={"amount_ml": 100, "lease_seconds": 300},
            )
            with lock:
                results.append(r)

    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(worker, range(n)))

    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 20
    assert codes.count(409) == 30
    assert {r.json()["error"]["code"] for r in results if r.status_code == 409} == {
        "insufficient_balance"
    }
    tokens = [r.json()["token"] for r in results if r.status_code == 201]
    assert len(set(tokens)) == 20

    b = get_balance(bid)
    assert (b["available_ml"], b["reserved_ml"], b["confirmed_ml"]) == (0, 2000, 0)


def test_concurrent_duplicate_confirms_collapse_to_one(base_url, make_batch, get_balance):
    bid = make_batch(10_000)
    with httpx.Client(base_url=base_url) as c:
        token = c.post(
            f"/batches/{bid}/reservations",
            json={"amount_ml": 500, "lease_seconds": 300},
        ).json()["token"]

    n = 20
    barrier = threading.Barrier(n)
    outcomes = []
    lock = threading.Lock()

    def worker(_):
        barrier.wait()
        with httpx.Client(base_url=base_url, timeout=10) as c:
            r = c.post(f"/batches/{bid}/reservations/{token}/confirm")
            with lock:
                outcomes.append(r)

    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(worker, range(n)))

    assert all(r.status_code == 200 for r in outcomes)
    assert {r.json()["confirmed_at"] for r in outcomes} == {
        outcomes[0].json()["confirmed_at"]
    }
    b = get_balance(bid)
    assert b["confirmed_ml"] == 500


def test_concurrent_confirm_vs_cancel_has_single_winner(base_url, make_batch, get_balance):
    bid = make_batch(10_000)
    with httpx.Client(base_url=base_url) as c:
        token = c.post(
            f"/batches/{bid}/reservations",
            json={"amount_ml": 700, "lease_seconds": 300},
        ).json()["token"]

    n = 24
    barrier = threading.Barrier(n)
    outcomes = []
    lock = threading.Lock()

    def worker(i):
        action = "confirm" if i % 2 == 0 else "cancel"
        barrier.wait()
        with httpx.Client(base_url=base_url, timeout=10) as c:
            r = c.post(f"/batches/{bid}/reservations/{token}/{action}")
            with lock:
                outcomes.append((action, r))

    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(worker, range(n)))

    with httpx.Client(base_url=base_url) as c:
        final = c.get(f"/batches/{bid}/reservations/{token}").json()
    assert final["status"] in {"confirmed", "cancelled"}
    for action, r in outcomes:
        if r.status_code == 200:
            assert r.json()["status"] == final["status"]
        else:
            code = r.json()["error"]["code"]
            if final["status"] == "confirmed":
                assert code == "reservation_already_confirmed"
            else:
                assert code == "reservation_cancelled"

    b = get_balance(bid)
    if final["status"] == "confirmed":
        assert (b["available_ml"], b["confirmed_ml"]) == (9300, 700)
    else:
        assert (b["available_ml"], b["confirmed_ml"]) == (10_000, 0)


def test_balance_conserved_through_mixed_sequence(client, make_batch, get_balance):
    bid = make_batch(600)
    # reserve 350, cancel 100 -> available 350, reserved 250
    t1 = client.post(
        f"/batches/{bid}/reservations", json={"amount_ml": 250, "lease_seconds": 300}
    ).json()["token"]
    t2 = client.post(
        f"/batches/{bid}/reservations", json={"amount_ml": 100, "lease_seconds": 300}
    ).json()["token"]
    get_balance(bid)  # query conserves
    assert client.post(f"/batches/{bid}/reservations/{t2}/cancel").status_code == 200
    b = get_balance(bid)
    assert (b["available_ml"], b["reserved_ml"], b["confirmed_ml"]) == (350, 250, 0)

    # reserve the freed 100, confirm t1 and the new one
    t3 = client.post(
        f"/batches/{bid}/reservations", json={"amount_ml": 100, "lease_seconds": 300}
    ).json()["token"]
    assert client.post(f"/batches/{bid}/reservations/{t1}/confirm").status_code == 200
    assert client.post(f"/batches/{bid}/reservations/{t3}/confirm").status_code == 200
    b = get_balance(bid)
    assert (b["available_ml"], b["reserved_ml"], b["confirmed_ml"]) == (250, 0, 350)

    # no room left beyond available
    over = client.post(
        f"/batches/{bid}/reservations", json={"amount_ml": 251, "lease_seconds": 300}
    )
    assert over.status_code == 409
    assert get_balance(bid)["available_ml"] == 250
