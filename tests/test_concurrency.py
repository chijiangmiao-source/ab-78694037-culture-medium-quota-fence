"""Concurrency tests against a real PostgreSQL + live uvicorn server."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from tests.conftest import assert_conserved


def _post(base, path, json=None):
    with httpx.Client(base_url=base, timeout=30) as client:
        return client.post(path, json=json)


def test_concurrent_reservations_never_overcommit(live_server, admin):
    base = live_server
    cap = 500
    with httpx.Client(base_url=base) as client:
        bid = client.post("/batches", json={"total_ml": cap}).json()["id"]

    n_callers, each = 25, 100
    with ThreadPoolExecutor(max_workers=n_callers) as pool:
        futures = [
            pool.submit(_post, base,
                        f"/batches/{bid}/reservations",
                        {"amount_ml": each, "lease_seconds": 300})
            for _ in range(n_callers)
        ]
        responses = [f.result() for f in as_completed(futures)]

    granted = [r for r in responses if r.status_code == 201]
    refused = [r for r in responses if r.status_code == 409]
    assert len(granted) == cap // each
    assert len(refused) == n_callers - cap // each
    assert all(r.json()["error"]["code"] == "insufficient_quota"
               for r in refused)
    tokens = [r.json()["fence_token"] for r in granted]
    assert len(tokens) == len(set(tokens))

    b = assert_conserved(admin, bid)
    assert b["available"] == 0
    assert b["reserved"] == cap


def test_concurrent_reservations_with_partial_amounts(live_server, admin):
    base = live_server
    with httpx.Client(base_url=base) as client:
        bid = client.post("/batches",
                          json={"total_ml": 337}).json()["id"]
    sizes = [60] * 10  # 600 requested, only 337 available
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [
            pool.submit(_post, base, f"/batches/{bid}/reservations",
                        {"amount_ml": size, "lease_seconds": 300})
            for size in sizes
        ]
        responses = [f.result() for f in as_completed(futures)]
    granted = [r for r in responses if r.status_code == 201]
    # floor(337/60) = 5 grants = 300ml, remainder 37 insufficient.
    assert len(granted) == 5
    b = assert_conserved(admin, bid)
    assert b["reserved"] == 300
    assert b["available"] == 37


def test_concurrent_duplicate_confirms_single_effect(live_server, admin):
    """Late acks racing the first confirm must not double-debit."""
    base = live_server
    with httpx.Client(base_url=base) as client:
        bid = client.post("/batches", json={"total_ml": 200}).json()["id"]
        token = client.post(
            f"/batches/{bid}/reservations",
            json={"amount_ml": 150, "lease_seconds": 300},
        ).json()["fence_token"]

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [
            pool.submit(_post, base,
                        f"/reservations/{token}/confirm")
            for _ in range(16)
        ]
        responses = [f.result() for f in as_completed(futures)]

    assert all(r.status_code == 200 for r in responses)
    bodies = [r.json() for r in responses]
    assert all(b["status"] == "confirmed" for b in bodies)
    confirmed_ats = {b["confirmed_at"] for b in bodies}
    assert len(confirmed_ats) == 1
    assert all(b["batch"]["confirmed_ml"] == 150 for b in bodies)
    assert all(b["batch"]["reserved_ml"] == 0 for b in bodies)
    assert all(b["batch"]["available_ml"] == 50 for b in bodies)
    b = assert_conserved(admin, bid)
    assert b["confirmed"] == 150
    assert admin.execute(
        "SELECT count(*) FROM reservation WHERE fence_token = %s AND "
        "status = 'confirmed'", (token,),
    ).fetchone()[0] == 1


def test_confirm_race_with_cancel_has_single_outcome(live_server, admin):
    base = live_server
    with httpx.Client(base_url=base) as client:
        bid = client.post("/batches", json={"total_ml": 100}).json()["id"]
        token = client.post(
            f"/batches/{bid}/reservations",
            json={"amount_ml": 100, "lease_seconds": 300},
        ).json()["fence_token"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_confirm = pool.submit(_post, base,
                                f"/reservations/{token}/confirm")
        f_cancel = pool.submit(_post, base,
                               f"/reservations/{token}/cancel")
        rc = f_confirm.result()
        rx = f_cancel.result()

    terminal = {rc.status_code: rc.json(), rx.status_code: rx.json()}
    # Exactly one terminal state wins.
    final_status = admin.execute(
        "SELECT status FROM reservation WHERE fence_token = %s",
        (token,),
    ).fetchone()[0]
    assert final_status in {"confirmed", "cancelled"}
    if final_status == "confirmed":
        assert rc.status_code == 200 and rc.json()["status"] == "confirmed"
        assert rx.status_code == 409
        assert rx.json()["error"]["code"] == (
            "reservation_already_confirmed")
    else:
        assert rx.status_code == 200 and rx.json()["status"] == "cancelled"
        assert rc.status_code == 409
        assert rc.json()["error"]["code"] == "reservation_cancelled"
    b = assert_conserved(admin, bid)
    assert terminal  # used for readability
    if final_status == "confirmed":
        assert b["confirmed"] == 100 and b["available"] == 0
    else:
        assert b["available"] == 100 and b["confirmed"] == 0


def test_concurrent_cross_batches_do_not_block_each_other(live_server, admin):
    base = live_server
    with httpx.Client(base_url=base) as client:
        b1 = client.post("/batches", json={"total_ml": 100}).json()["id"]
        b2 = client.post("/batches", json={"total_ml": 100}).json()["id"]

    def reserve(bid):
        return _post(base, f"/batches/{bid}/reservations",
                     {"amount_ml": 100, "lease_seconds": 300})

    with ThreadPoolExecutor(max_workers=8) as pool:
        rs = list(pool.map(reserve, [b1, b2] * 4))
    assert [r.status_code for r in rs].count(201) == 2
    assert_conserved(admin, b1)
    assert_conserved(admin, b2)
