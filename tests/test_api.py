"""HTTP-level tests: structured errors, envelopes, immutability, idempotency."""
from __future__ import annotations

from datetime import timedelta

from app import repository

from .conftest import assert_conserved, fetch_batch, freeze_at


def _batch(client, total=1000):
    r = client.post("/batches", json={"total_ml": total})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok"}


def test_create_and_get_batch(client):
    r = client.post("/batches", json={"total_ml": 420})
    assert r.status_code == 201
    body = r.json()
    assert body["total_ml"] == 420
    assert body["available_ml"] == 420
    assert body["reserved_ml"] == 0
    assert body["confirmed_ml"] == 0

    g = client.get(f"/batches/{body['id']}")
    assert g.status_code == 200
    assert g.json()["total_ml"] == 420


def test_structured_validation_error(client):
    r = client.post("/batches", json={"total_ml": 0})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert isinstance(err["message"], str) and err["message"]
    assert "details" in err and "issues" in err["details"]


def test_structured_not_found(client):
    r = client.get("/batches/777")
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "batch_not_found"
    assert err["message"]


def test_reservation_full_lifecycle(client, pool):
    bid = _batch(client, 1000)

    r = client.post(f"/batches/{bid}/reservations", json={"amount_ml": 300, "lease_seconds": 60})
    assert r.status_code == 201, r.text
    first = r.json()
    token = first["token"]
    assert first["status"] == "held"
    assert isinstance(token, int)

    g = client.get(f"/batches/{bid}/reservations/{token}")
    assert g.status_code == 200
    assert g.json()["status"] == "held"

    c = client.post(f"/batches/{bid}/reservations/{token}/confirm")
    assert c.status_code == 200
    confirmed = c.json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_at"] is not None

    # Idempotent replay over HTTP returns the identical original result.
    c2 = client.post(f"/batches/{bid}/reservations/{token}/confirm")
    assert c2.status_code == 200
    again = c2.json()
    assert again["status"] == "confirmed"
    assert again["confirmed_at"] == confirmed["confirmed_at"]

    # Confirm after cancel is impossible once confirmed.
    x = client.post(f"/batches/{bid}/reservations/{token}/cancel")
    assert x.status_code == 409
    assert x.json()["error"]["code"] == "reservation_already_confirmed"

    b = assert_conserved(pool, bid)
    assert (b["available_ml"], b["reserved_ml"], b["confirmed_ml"]) == (700, 0, 300)


def test_cancel_then_confirm_permanently_rejected(client, pool):
    bid = _batch(client)
    token = client.post(
        f"/batches/{bid}/reservations", json={"amount_ml": 200, "lease_seconds": 60}
    ).json()["token"]

    x = client.post(f"/batches/{bid}/reservations/{token}/cancel")
    assert x.status_code == 200
    assert x.json()["status"] == "cancelled"

    y = client.post(f"/batches/{bid}/reservations/{token}/confirm")
    assert y.status_code == 409
    assert y.json()["error"]["code"] == "reservation_cancelled"

    # Again later: still rejected.
    z = client.post(f"/batches/{bid}/reservations/{token}/confirm")
    assert z.status_code == 409
    assert z.json()["error"]["code"] == "reservation_cancelled"
    assert_conserved(pool, bid)


def test_insufficient_balance_structured(client, pool):
    bid = _batch(client, 100)
    ok = client.post(f"/batches/{bid}/reservations", json={"amount_ml": 80, "lease_seconds": 60})
    assert ok.status_code == 201

    bad = client.post(f"/batches/{bid}/reservations", json={"amount_ml": 21, "lease_seconds": 60})
    assert bad.status_code == 409
    err = bad.json()["error"]
    assert err["code"] == "insufficient_balance"
    assert err["details"]["requested_ml"] == 21
    assert err["details"]["available_ml"] == 20
    assert_conserved(pool, bid)


def test_lease_validation_http(client):
    bid = _batch(client)
    r1 = client.post(f"/batches/{bid}/reservations", json={"amount_ml": 1, "lease_seconds": 4})
    assert r1.status_code == 422
    r2 = client.post(f"/batches/{bid}/reservations", json={"amount_ml": 1, "lease_seconds": 301})
    assert r2.status_code == 422


def test_fence_tokens_monotonic_http(client):
    bid = _batch(client, 100_000)
    tokens = []
    for _ in range(10):
        r = client.post(f"/batches/{bid}/reservations", json={"amount_ml": 1, "lease_seconds": 300})
        tokens.append(r.json()["token"])
    assert all(b > a for a, b in zip(tokens, tokens[1:]))


def test_expiry_via_settlement_http(client, pool):
    bid = _batch(client, 100)
    with pool.connection() as conn:
        r = repository.reserve(conn, bid, 40, 5)
        t0, token = r["created_at"], r["token"]

    # Settle at a frozen DB instant past the deadline, then observe via HTTP.
    with pool.connection() as conn:
        freeze_at(conn, t0 + timedelta(seconds=6))
        repository.get_batch(conn, bid)

    late = client.post(f"/batches/{bid}/reservations/{token}/confirm")
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "reservation_expired"
    assert_conserved(pool, bid)


def test_balance_visible_after_each_query(client, pool):
    bid = _batch(client, 300)
    token = client.post(
        f"/batches/{bid}/reservations", json={"amount_ml": 120, "lease_seconds": 60}
    ).json()["token"]
    b = client.get(f"/batches/{bid}").json()
    assert b["total_ml"] == b["available_ml"] + b["reserved_ml"] + b["confirmed_ml"]

    client.post(f"/batches/{bid}/reservations/{token}/confirm")
    b = client.get(f"/batches/{bid}").json()
    assert b == {
        **b,
        "available_ml": 180,
        "reserved_ml": 0,
        "confirmed_ml": 120,
    }
    assert b["total_ml"] == 300
    assert fetch_batch(pool, bid)["total_ml"] == 300
