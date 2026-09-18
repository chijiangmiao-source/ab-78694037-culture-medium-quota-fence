"""Invariant, real-time expiry, and randomized interleaving tests."""
from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from tests.conftest import assert_conserved


# ---------------------------------------------------------------------------
# Real wall-clock expiry through the database clock (minimum lease is 5s)
# ---------------------------------------------------------------------------

def test_real_wall_clock_expiry_releases_and_blocks_confirm(live_server,
                                                            admin):
    base = live_server
    with httpx.Client(base_url=base, timeout=30) as client:
        bid = client.post("/batches", json={"total_ml": 100}).json()["id"]
        token = client.post(
            f"/batches/{bid}/reservations",
            json={"amount_ml": 70, "lease_seconds": 5},
        ).json()["fence_token"]

        # Valid strictly before expiry.
        view = client.get(f"/batches/{bid}").json()
        assert view["reserved_ml"] == 70 and view["available_ml"] == 30

        time.sleep(6.5)  # expires_at is definitely <= now()

        resp = client.post(f"/reservations/{token}/confirm")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "reservation_expired"

        # Settlement released quota and it is immediately reusable.
        view = client.get(f"/batches/{bid}").json()
        assert view["available_ml"] == 100
        assert view["reserved_ml"] == 0
        assert view["total_ml"] == view["available_ml"] + view[
            "reserved_ml"] + view["confirmed_ml"]

        again = client.post(
            f"/batches/{bid}/reservations",
            json={"amount_ml": 100, "lease_seconds": 300},
        )
        assert again.status_code == 201
        assert again.json()["fence_token"] > token

        # Permanently dead token.
        assert client.post(
            f"/reservations/{token}/confirm").status_code == 409
        assert_conserved(admin, bid)


# ---------------------------------------------------------------------------
# Conservation must hold on *every* query, including mid-flight mutations
# ---------------------------------------------------------------------------

def test_conservation_on_each_query_during_writes(live_server, admin):
    base = live_server
    stop = threading.Event()
    violations: list[dict] = []

    with httpx.Client(base_url=base) as client:
        bid = client.post("/batches",
                          json={"total_ml": 1000}).json()["id"]

    def writer(seed: int):
        rng = random.Random(seed)
        with httpx.Client(base_url=base, timeout=30) as http:
            active: list[int] = []
            end = time.time() + 8
            while time.time() < end and not stop.is_set():
                action = rng.choices(
                    ["reserve", "confirm", "cancel", "noop"],
                    weights=[5, 2, 2, 1],
                )[0]
                try:
                    if action == "reserve":
                        r = http.post(
                            f"/batches/{bid}/reservations",
                            json={"amount_ml": rng.randint(1, 120),
                                  "lease_seconds": rng.randint(5, 300)},
                        )
                        if r.status_code == 201:
                            active.append(r.json()["fence_token"])
                    elif action == "confirm" and active:
                        token = active.pop(rng.randrange(len(active)))
                        http.post(f"/reservations/{token}/confirm")
                    elif action == "cancel" and active:
                        token = active.pop(rng.randrange(len(active)))
                        http.post(f"/reservations/{token}/cancel")
                except httpx.HTTPError:
                    pass
                time.sleep(rng.uniform(0, 0.02))

    def reader():
        with httpx.Client(base_url=base, timeout=30) as http:
            while not stop.is_set():
                body = http.get(f"/batches/{bid}").json()
                if body["total_ml"] != (body["available_ml"]
                                        + body["reserved_ml"]
                                        + body["confirmed_ml"]):
                    violations.append(body)
                # None of the components may ever be negative.
                if min(body["available_ml"], body["reserved_ml"],
                       body["confirmed_ml"]) < 0:
                    violations.append(body)

    threads = [threading.Thread(target=writer, args=(i,))
               for i in range(6)]
    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads + readers:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    for t in readers:
        t.join()

    assert violations == []
    # After the storm, ledger and counters must reconcile exactly.
    settled = assert_conserved(admin, bid)
    assert settled["total"] == 1000


def test_mixed_concurrent_confirm_and_cancel_never_double_debits(
        live_server, admin):
    """Many held leases, each racing confirm vs cancel: each terminates once."""
    base = live_server
    n = 20
    with httpx.Client(base_url=base) as client:
        bid = client.post("/batches",
                          json={"total_ml": 100 * n}).json()["id"]
        tokens = [
            client.post(
                f"/batches/{bid}/reservations",
                json={"amount_ml": 100, "lease_seconds": 300},
            ).json()["fence_token"]
            for _ in range(n)
        ]

    def hit(token: int, kind: str):
        with httpx.Client(base_url=base) as http:
            return token, kind, http.post(
                f"/reservations/{token}/{kind}")

    jobs = [(t, k) for t in tokens for k in ("confirm", "cancel")]
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = [f.result() for f in
                   as_completed(pool.submit(hit, t, k) for t, k in jobs)]

    by_token: dict[int, dict] = {}
    for token, kind, resp in results:
        by_token.setdefault(token, {})[kind] = resp

    confirmed_count = 0
    for token, pair in by_token.items():
        codes = {k: v.status_code for k, v in pair.items()}
        # One terminal operation wins (200); the other is rejected (409).
        assert sorted(codes.values()) == [200, 409], (token, codes)
        winner_status = admin.execute(
            "SELECT status FROM reservation WHERE fence_token = %s",
            (token,),
        ).fetchone()[0]
        assert winner_status in {"confirmed", "cancelled"}
        if pair["confirm"].status_code == 200:
            assert pair["confirm"].json()["status"] == "confirmed"
            assert winner_status == "confirmed"
            confirmed_count += 1
        else:
            assert pair["cancel"].json()["status"] == "cancelled"
            assert winner_status == "cancelled"

    final = assert_conserved(admin, bid)
    assert final["confirmed"] == 100 * confirmed_count
    # Cancelled leases give everything back; remaining valid leases held.
    assert final["available"] == 100 * (n - confirmed_count)
    assert final["reserved"] == 0
