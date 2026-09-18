"""Functional tests for batch lifecycle and structured errors."""
from __future__ import annotations

import psycopg
import pytest

from tests.conftest import assert_conserved


def make_batch(client, total=1000):
    resp = client.post("/batches", json={"total_ml": total})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_batch_initial_state(client):
    b = make_batch(client)
    assert b["total_ml"] == 1000
    assert b["available_ml"] == 1000
    assert b["reserved_ml"] == 0
    assert b["confirmed_ml"] == 0


def test_batch_total_immutable_via_api_update_path(client, admin):
    b = make_batch(client, 500)
    # There is deliberately no update endpoint; attempt direct DB writes.
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            "UPDATE batch SET total_ml = %s WHERE id = %s",
            (501, b["id"]),
        )


def test_get_batch_settles_expired_before_reading(client, admin):
    b = make_batch(client, 200)
    r = client.post(
        f"/batches/{b['id']}/reservations",
        json={"amount_ml": 80, "lease_seconds": 5},
    ).json()
    admin.execute(
        "UPDATE reservation SET expires_at = now() WHERE fence_token = %s",
        (r["fence_token"],),
    )
    got = client.get(f"/batches/{b['id']}").json()
    assert got["available_ml"] == 200
    assert got["reserved_ml"] == 0
    assert got["confirmed_ml"] == 0


def test_unknown_batch_and_reservation(client):
    resp = client.get("/batches/99999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "batch_not_found"

    resp = client.post("/reservations/99999/confirm")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "reservation_not_found"

    resp = client.post("/reservations/99999/cancel")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "reservation_not_found"


def test_invalid_total_structured(client):
    for payload in ({"total_ml": 0}, {"total_ml": -1}, {"total_ml": 1.5}):
        resp = client.post("/batches", json=payload)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        assert resp.json()["error"]["details"]["issues"]


def test_reservation_input_validation(client):
    b = make_batch(client)
    base = f"/batches/{b['id']}/reservations"
    cases = [
        ({"amount_ml": 0, "lease_seconds": 60}, 422),
        ({"amount_ml": -5, "lease_seconds": 60}, 422),
        ({"amount_ml": 10, "lease_seconds": 4}, 422),
        ({"amount_ml": 10, "lease_seconds": 301}, 422),
        ({"amount_ml": 10, "lease_seconds": 5}, 201),
        ({"amount_ml": 10, "lease_seconds": 300}, 201),
    ]
    for payload, expected in cases:
        resp = client.post(base, json=payload)
        assert resp.status_code == expected, (payload, resp.text)
        if expected == 422:
            assert resp.json()["error"]["code"] == "validation_error"


def test_reservation_on_missing_batch(client):
    resp = client.post(
        "/batches/77777/reservations",
        json={"amount_ml": 10, "lease_seconds": 60},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "batch_not_found"


def test_fence_tokens_strictly_increasing_and_never_reused(client, admin):
    b = make_batch(client, 1000)
    tokens = []
    for amount in (100, 200, 50):
        r = client.post(
            f"/batches/{b['id']}/reservations",
            json={"amount_ml": amount, "lease_seconds": 60},
        )
        assert r.status_code == 201
        tokens.append(r.json()["fence_token"])
    assert tokens == sorted(tokens)
    assert len(set(tokens)) == 3
    # Cancel one and expire another; the sequence must still move forward.
    client.post(f"/reservations/{tokens[0]}/cancel")
    admin.execute(
        "UPDATE reservation SET expires_at = now() WHERE fence_token = %s",
        (tokens[1],),
    )
    client.get(f"/batches/{b['id']}")  # settle
    r = client.post(
        f"/batches/{b['id']}/reservations",
        json={"amount_ml": 10, "lease_seconds": 60},
    )
    new_token = r.json()["fence_token"]
    assert new_token > max(tokens)
    # No token value appears twice anywhere.
    total, distinct = admin.execute(
        "SELECT count(*), count(DISTINCT fence_token) FROM reservation"
    ).fetchone()
    assert total == distinct


def test_conservation_after_basic_flow(client, admin):
    b = make_batch(client, 1000)
    bid = b["id"]
    r = client.post(
        f"/batches/{bid}/reservations",
        json={"amount_ml": 300, "lease_seconds": 60},
    ).json()
    assert_conserved(admin, bid)

    client.post(f"/reservations/{r['fence_token']}/confirm")
    assert_conserved(admin, bid)

    r2 = client.post(
        f"/batches/{bid}/reservations",
        json={"amount_ml": 200, "lease_seconds": 60},
    ).json()
    client.post(f"/reservations/{r2['fence_token']}/cancel")
    assert_conserved(admin, bid)
