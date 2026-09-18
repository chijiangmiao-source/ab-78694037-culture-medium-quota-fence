"""Reserve/confirm/cancel state machine and edge semantics."""
from __future__ import annotations

import pytest

from app.errors import (
    ReservationCancelled,
    ReservationExpired,
)
from app.service import (
    cancel_reservation_tx,
    confirm_reservation_tx,
    create_batch_tx,
    reserve_quota_tx,
)
from tests.conftest import assert_conserved


def make_batch(client, total=1000):
    return client.post("/batches", json={"total_ml": total}).json()


def reserve(client, bid, amount, lease=60):
    return client.post(
        f"/batches/{bid}/reservations",
        json={"amount_ml": amount, "lease_seconds": lease},
    )


def test_insufficient_quota_structured(client, admin):
    b = make_batch(client, 300)
    r = reserve(client, b["id"], 250).json()
    assert_conserved(admin, b["id"])

    resp = reserve(client, b["id"], 51)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "insufficient_quota"
    assert body["error"]["details"]["requested_ml"] == 51
    assert body["error"]["details"]["available_ml"] == 50
    assert_conserved(admin, b["id"])

    # Exactly the remaining amount still succeeds.
    ok = reserve(client, b["id"], 50)
    assert ok.status_code == 201
    client.post(f"/reservations/{r['fence_token']}/cancel")


def test_confirm_moves_reserved_to_confirmed(client, admin):
    b = make_batch(client, 500)
    r = reserve(client, b["id"], 200).json()
    resp = client.post(f"/reservations/{r['fence_token']}/confirm")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["confirmed_at"] is not None
    assert body["batch"]["reserved_ml"] == 0
    assert body["batch"]["confirmed_ml"] == 200
    assert body["batch"]["available_ml"] == 300
    assert_conserved(admin, b["id"])


def test_confirm_is_idempotent_returns_original(client, admin):
    b = make_batch(client, 500)
    token = reserve(client, b["id"], 120).json()["fence_token"]
    first = client.post(f"/reservations/{token}/confirm").json()
    second = client.post(f"/reservations/{token}/confirm").json()
    third = client.post(f"/reservations/{token}/confirm").json()
    for body in (second, third):
        assert body["status"] == "confirmed"
        assert body["confirmed_at"] == first["confirmed_at"]
        assert body["batch"]["confirmed_ml"] == 120
    # Ledger holds a single confirmed reservation.
    assert admin.execute(
        "SELECT count(*) FROM reservation WHERE fence_token = %s "
        "AND status = 'confirmed'", (token,),
    ).fetchone()[0] == 1
    assert_conserved(admin, b["id"])


def test_cancel_releases_quota_and_blocks_confirm(client, admin):
    b = make_batch(client, 400)
    token = reserve(client, b["id"], 300).json()["fence_token"]
    resp = client.post(f"/reservations/{token}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["batch"]["available_ml"] == 400
    assert resp.json()["batch"]["reserved_ml"] == 0

    again = client.post(f"/reservations/{token}/confirm")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "reservation_cancelled"
    assert_conserved(admin, b["id"])

    # Released quota is usable by a new reservation.
    r2 = reserve(client, b["id"], 400).json()
    assert r2["fence_token"] > token
    client.post(f"/reservations/{r2['fence_token']}/confirm")
    assert_conserved(admin, b["id"])


def test_cancel_idempotent(client, admin):
    b = make_batch(client, 100)
    token = reserve(client, b["id"], 40).json()["fence_token"]
    first = client.post(f"/reservations/{token}/cancel").json()
    second = client.post(f"/reservations/{token}/cancel").json()
    assert first["status"] == second["status"] == "cancelled"
    assert first["cancelled_at"] == second["cancelled_at"]
    assert_conserved(admin, b["id"])


def test_cannot_cancel_confirmed(client, admin):
    b = make_batch(client, 100)
    token = reserve(client, b["id"], 40).json()["fence_token"]
    client.post(f"/reservations/{token}/confirm")
    resp = client.post(f"/reservations/{token}/cancel")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "reservation_already_confirmed"
    assert_conserved(admin, b["id"])


def test_expired_at_exact_equal_is_rejected(pool, admin):
    """Critical boundary: now() == expires_at means expired, not valid.

    PostgreSQL ``now()`` is frozen for the whole transaction, so pinning
    expires_at to ``now()`` and then confirming in that same transaction
    observes the equality point deterministically.
    """
    with pool.connection() as conn:
        with conn.transaction():
            batch = create_batch_tx(conn, 100)
            bid = batch["id"]
            held = reserve_quota_tx(conn, bid, 60, lease_seconds=300)
            token = held["fence_token"]
            conn.execute(
                "UPDATE reservation SET expires_at = now() "
                "WHERE fence_token = %s",
                (token,),
            )
            with pytest.raises(ReservationExpired):
                confirm_reservation_tx(conn, token)
            # Still inside this transaction: the settlement fired above.
            row = conn.execute(
                "SELECT status FROM reservation WHERE fence_token = %s",
                (token,),
            ).fetchone()
            assert row["status"] == "expired"
            b = conn.execute(
                "SELECT available_ml, reserved_ml, confirmed_ml "
                "FROM batch WHERE id = %s",
                (bid,),
            ).fetchone()
            assert (b["available_ml"], b["reserved_ml"],
                    b["confirmed_ml"]) == (100, 0, 0)
    assert_conserved(admin, bid)

    # Reconfirming a now-committed expired token is refused forever.
    with pool.connection() as conn:
        with conn.transaction():
            with pytest.raises(ReservationExpired):
                confirm_reservation_tx(conn, token)


def test_still_valid_when_strictly_earlier_than_expiry(pool, admin):
    """A lease valid strictly before expires_at must still confirm.

    Same transaction trick: one microsecond of headroom keeps the lease
    strictly later than now(), so confirmation must succeed.
    """
    with pool.connection() as conn:
        with conn.transaction():
            batch = create_batch_tx(conn, 100)
            bid = batch["id"]
            held = reserve_quota_tx(conn, bid, 30, lease_seconds=300)
            token = held["fence_token"]
            conn.execute(
                "UPDATE reservation SET expires_at = now() + interval "
                "'1 microsecond' WHERE fence_token = %s",
                (token,),
            )
            result = confirm_reservation_tx(conn, token)
            assert result["status"] == "confirmed"
            assert result["batch"]["confirmed_ml"] == 30
            assert result["batch"]["reserved_ml"] == 0
            assert result["batch"]["available_ml"] == 70
    assert_conserved(admin, bid)


def test_cancel_of_held_while_unexpired_in_tx(pool, admin):
    with pool.connection() as conn:
        with conn.transaction():
            bid = create_batch_tx(conn, 100)["id"]
            token = reserve_quota_tx(conn, bid, 40, lease_seconds=300)[
                "fence_token"]
            out = cancel_reservation_tx(conn, token)
            assert out["status"] == "cancelled"
            with pytest.raises(ReservationCancelled):
                confirm_reservation_tx(conn, token)
    assert_conserved(admin, bid)


def test_expired_settlement_releases_for_new_reservation(client, admin):
    b = make_batch(client, 100)
    t1 = reserve(client, b["id"], 100, lease=5).json()["fence_token"]
    admin.execute(
        "UPDATE reservation SET expires_at = now() WHERE fence_token = %s",
        (t1,),
    )
    # The new reservation settles t1 within the same transaction first.
    r2 = reserve(client, b["id"], 100, lease=60)
    assert r2.status_code == 201, r2.text
    assert r2.json()["fence_token"] > t1
    assert r2.json()["batch"]["available_ml"] == 0
    assert r2.json()["batch"]["reserved_ml"] == 100
    assert_conserved(admin, b["id"])


def test_cancel_after_expiry_reports_expired(client, admin):
    b = make_batch(client, 100)
    token = reserve(client, b["id"], 50, lease=5).json()["fence_token"]
    admin.execute(
        "UPDATE reservation SET expires_at = now() WHERE fence_token = %s",
        (token,),
    )
    resp = client.post(f"/reservations/{token}/cancel")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "reservation_expired"
    assert_conserved(admin, b["id"])


def test_get_reservation_settles_status(client, admin):
    b = make_batch(client, 100)
    token = reserve(client, b["id"], 10, lease=5).json()["fence_token"]
    admin.execute(
        "UPDATE reservation SET expires_at = now() WHERE fence_token = %s",
        (token,),
    )
    got = client.get(f"/reservations/{token}").json()
    assert got["status"] == "expired"
