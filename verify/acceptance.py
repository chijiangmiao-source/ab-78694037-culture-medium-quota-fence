"""One-shot acceptance gate for the quota stack.

Runs end-to-end against the API and cross-checks the database directly.
Exit code 0 only if every business rule and the conservation invariant
hold. Used by the ``verify`` docker compose service.

Required environment:
    API_BASE_URL  e.g. http://api:8000
    DATABASE_URL  libpq DSN for direct invariant checks
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg

API = os.environ.get("API_BASE_URL", "http://api:8000").rstrip("/")
DSN = os.environ["DATABASE_URL"].replace("postgresql+psycopg://",
                                        "postgresql://", 1)

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        _failures.append(name)


def request(method: str, path: str, body: dict | None = None,
            expect: int | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            code = resp.status
            payload = json.loads(resp.read() or "null")
    except urllib.error.HTTPError as exc:
        code = exc.code
        payload = json.loads(exc.read() or "null")
    if expect is not None:
        check(f"HTTP {method} {path} -> {expect}", code == expect,
              f"got {code}: {payload}")
    return code, payload


def db_balance(conn, batch_id: int) -> dict:
    row = conn.execute(
        "SELECT total_ml, available_ml, reserved_ml, confirmed_ml "
        "FROM batch WHERE id = %s", (batch_id,),
    ).fetchone()
    return {"total": row[0], "available": row[1],
            "reserved": row[2], "confirmed": row[3]}


def assert_conserved(conn, batch_id: int, label: str) -> None:
    b = db_balance(conn, batch_id)
    check(f"conservation after {label}",
          b["total"] == b["available"] + b["reserved"] + b["confirmed"],
          json.dumps(b))
    # Ledger reconciliation: counters must equal the reservation ledger.
    led = conn.execute(
        """
        SELECT COALESCE(SUM(amount_ml) FILTER (WHERE status='held'), 0),
               COALESCE(SUM(amount_ml) FILTER (WHERE status='confirmed'), 0)
          FROM reservation WHERE batch_id = %s
        """,
        (batch_id,),
    ).fetchone()
    check(f"ledger reconciliation after {label}",
          led[0] == b["reserved"] and led[1] == b["confirmed"],
          f"held={led[0]} confirmed={led[1]} vs counters {b}")


def main() -> int:
    # Wait for the API to be reachable.
    for _ in range(60):
        try:
            code, body = request("GET", "/health", expect=200)
            break
        except Exception:
            time.sleep(1)
    else:
        print("API never became healthy")
        return 1

    with psycopg.connect(DSN, autocommit=True) as conn:
        # --- batch creation, immutable total -----------------------------
        _, batch = request("POST", "/batches", {"total_ml": 1000}, 201)
        bid = batch["id"]
        check("batch starts fully available",
              batch["total_ml"] == 1000 and batch["available_ml"] == 1000
              and batch["reserved_ml"] == 0 and batch["confirmed_ml"] == 0)
        try:
            conn.execute("UPDATE batch SET total_ml = 9999 WHERE id = %s",
                         (bid,))
            immutable = False
        except psycopg.errors.CheckViolation:
            immutable = True
        check("total_ml immutable in database", immutable)

        # --- input validation is structured ------------------------------
        code, bad = request("POST", f"/batches/{bid}/reservations",
                            {"amount_ml": 0, "lease_seconds": 60}, 422)
        check("zero amount rejected structurally",
              code == 422 and bad["error"]["code"] == "validation_error")
        code, bad = request("POST", f"/batches/{bid}/reservations",
                            {"amount_ml": 10, "lease_seconds": 4}, 422)
        check("lease below 5s rejected", bad["error"]["code"]
              == "validation_error")
        code, bad = request("POST", f"/batches/{bid}/reservations",
                            {"amount_ml": 10, "lease_seconds": 301}, 422)
        check("lease above 300s rejected", bad["error"]["code"]
              == "validation_error")

        # --- reservation / insufficient quota ----------------------------
        _, r1 = request("POST", f"/batches/{bid}/reservations",
                        {"amount_ml": 600, "lease_seconds": 300}, 201)
        t1 = r1["fence_token"]
        check("reserve reports moved counters",
              r1["batch"]["available_ml"] == 400
              and r1["batch"]["reserved_ml"] == 600)
        assert_conserved(conn, bid, "first reservation")

        code, short = request("POST", f"/batches/{bid}/reservations",
                              {"amount_ml": 500, "lease_seconds": 300}, 409)
        check("over-reservation refused with structured error",
              short["error"]["code"] == "insufficient_quota"
              and short["error"]["details"]["available_ml"] == 400)

        _, r2 = request("POST", f"/batches/{bid}/reservations",
                        {"amount_ml": 400, "lease_seconds": 300}, 201)
        t2 = r2["fence_token"]
        check("fence tokens strictly increasing within batch", t2 > t1)

        # --- exactly one effective confirmation, idempotent replay -------
        _, c1 = request("POST", f"/reservations/{t1}/confirm", None, 200)
        _, c1b = request("POST", f"/reservations/{t1}/confirm", None, 200)
        check("duplicate confirm returns original result",
              c1["status"] == c1b["status"] == "confirmed"
              and c1["confirmed_at"] == c1b["confirmed_at"])
        assert_conserved(conn, bid, "confirm + replay")
        b = db_balance(conn, bid)
        check("confirmed once only", b["confirmed"] == 600
              and b["reserved"] == 400)

        # --- cancellation releases quota and is permanent ----------------
        _, x2 = request("POST", f"/reservations/{t2}/cancel", None, 200)
        check("cancel releases quota",
              x2["batch"]["available_ml"] == 400
              and x2["batch"]["reserved_ml"] == 0)
        code, after_cancel = request(
            "POST", f"/reservations/{t2}/confirm", None, 409)
        check("cancelled token permanently refused",
              after_cancel["error"]["code"] == "reservation_cancelled")
        code, _ = request("POST", f"/reservations/{t2}/confirm", None, 409)
        check("cancelled token refused again", code == 409)
        assert_conserved(conn, bid, "cancel")

        # --- expired lease: equality means expired -----------------------
        _, r3 = request("POST", f"/batches/{bid}/reservations",
                        {"amount_ml": 100, "lease_seconds": 300}, 201)
        t3 = r3["fence_token"]
        conn.execute(
            "UPDATE reservation SET expires_at = now() "
            "WHERE fence_token = %s", (t3,))
        code, expired = request(
            "POST", f"/reservations/{t3}/confirm", None, 409)
        check("confirm at expires_at == now() is treated as expired",
              expired["error"]["code"] == "reservation_expired")
        _, got = request("GET", f"/reservations/{t3}", None, 200)
        check("expired lease recorded and quota released",
              got["status"] == "expired")
        assert_conserved(conn, bid, "expiry settlement")
        code, _ = request("POST", f"/reservations/{t3}/confirm", None, 409)
        check("expired token permanently refused", code == 409)

        # expired quota can be reserved again; token still not reused
        _, r4 = request("POST", f"/batches/{bid}/reservations",
                        {"amount_ml": 100, "lease_seconds": 300}, 201)
        check("released quota reservable with a new token",
              r4["fence_token"] > t3)
        request("POST", f"/reservations/{r4['fence_token']}/cancel",
                None, 200)
        assert_conserved(conn, bid, "reuse of released quota")

    # --- concurrent contention storm -------------------------------------
    with psycopg.connect(DSN, autocommit=True) as conn:
        _, cb = request("POST", "/batches", {"total_ml": 500}, 201)
        cid = cb["id"]

        results: list[tuple[int, dict]] = []

        def hit(i: int):
            return request("POST", f"/batches/{cid}/reservations",
                           {"amount_ml": 100, "lease_seconds": 300})

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(hit, i) for i in range(20)]
            for f in as_completed(futures):
                results.append(f.result())
        ok = [p for code, p in results if code == 201]
        no = [p for code, p in results if code == 409]
        check("exactly capacity/allocation concurrent grants",
              len(ok) == 5 and len(no) == 15,
              f"granted={len(ok)} refused={len(no)}")
        tokens = [p["fence_token"] for c, p in results if c == 201]
        check("no duplicate fence tokens", len(tokens) == len(set(tokens)))
        assert_conserved(conn, cid, "concurrent reservation storm")

        # repeated concurrent confirm of one token -> one effective confirm
        token = ok[0]["fence_token"]

        def confirm():
            return request("POST", f"/reservations/{token}/confirm")

        with ThreadPoolExecutor(max_workers=12) as pool2:
            cresults = [f.result() for f in
                        (pool2.submit(confirm) for _ in range(12))]
        check("all concurrent confirms observe the single result",
              all(code == 200 and p["status"] == "confirmed"
                  and p["confirmed_at"] == cresults[0][1]["confirmed_at"]
                  for code, p in cresults))
        assert_conserved(conn, cid, "concurrent confirms")
        b = db_balance(conn, cid)
        check("only 100ml confirmed by 12 racing confirms",
              b["confirmed"] == 100 and b["reserved"] == 400)

    if _failures:
        print(f"\n{len(_failures)} acceptance check(s) FAILED:")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("\nALL ACCEPTANCE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
