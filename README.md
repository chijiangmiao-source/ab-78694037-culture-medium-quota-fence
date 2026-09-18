# Culture-medium Quota Service

Pure-backend **FastAPI + PostgreSQL** service for an automated liquid
preparation line. Devices first reserve millilitre quota against a batch
budget and then confirm the pump; the service guarantees that concurrent
devices can never over-commit a batch, that a late confirmation after
lease expiry (or after cancellation) is permanently rejected, and that
released quota is never debited twice.

## Invariants

All quantities are **integer millilitres**. Every batch permanently
maintains

```
total_ml = available_ml + reserved_ml + confirmed_ml
```

enforced by a database `CHECK` constraint (a transaction that would
break it cannot commit) and reconciled against the reservation ledger in
the acceptance and pytest suites.

* **Immutable total** — `total_ml` cannot change after batch creation;
  enforced by a row trigger, not just the absence of an endpoint.
* **Positive reservations** — `amount_ml` must be a positive integer;
  lease duration must be an integer in `[5, 300]` seconds.
* **Fence tokens** — every successful reservation returns a
  `fence_token` from a database identity sequence: strictly increasing
  within a batch and never reused, including for cancelled/expired rows.
* **Database clock only** — all deadlines are `now()` in PostgreSQL.
  `expires_at = now() + lease`. The application never supplies wall-clock
  time.
* **Exact expiry boundary** — confirmation succeeds only when the
  current database time is **strictly earlier** than `expires_at`;
  settlement uses `expires_at <= now()`, so equality means expired.
* **Idempotent confirmation** — resubmitting a confirmed token returns
  the original result (same `confirmed_at`). Cancelled or expired tokens
  are permanently refused for confirmation.
* **Settle-then-lock on every change** — each state-changing
  transaction, in one unit, first settles due reservations and then
  locks the batch row. Lock order is always *batch → reservations*, so
  same-batch mutators serialize without deadlock; a deadlock victim is
  retried as a safety net.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/batches` | Create a batch `{ "total_ml": 1000 }` |
| `GET`  | `/batches/{id}` | Current counters (settles due leases first) |
| `POST` | `/batches/{id}/reservations` | Reserve `{ "amount_ml": 600, "lease_seconds": 60 }` |
| `POST` | `/reservations/{fence_token}/confirm` | Confirm a held lease |
| `POST` | `/reservations/{fence_token}/cancel` | Cancel a held lease |
| `GET`  | `/reservations/{fence_token}` | Reservation status + batch counters |
| `GET`  | `/health` | Liveness |

Errors are structured and never mocked:

```json
{ "error": { "code": "insufficient_quota",
             "message": "not enough available quota for this reservation",
             "details": { "batch_id": 1, "requested_ml": 500,
                          "available_ml": 400 } } }
```

Codes: `batch_not_found` (404), `reservation_not_found` (404),
`validation_error` (422), `insufficient_quota` (409),
`reservation_expired` (409), `reservation_cancelled` (409),
`reservation_already_confirmed` (409).

## Running with Docker Compose

```bash
docker compose build
API_PORT=9000 docker compose up            # host port overridable via API_PORT
docker compose run --rm verify            # one-shot acceptance gate
```

`verify` is a one-shot service that drives the full reserve → confirm →
replay → cancel → exact-expiry → contention story over HTTP and
cross-checks conservation directly in PostgreSQL, exiting non-zero on
any violation.

## Running the tests locally

The pytest suite uses a **real PostgreSQL** (no mocks/fakes). Point it
at an admin DSN able to create databases:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export TEST_DATABASE_URL=postgresql://postgres@localhost:5432/postgres
pytest
```

The suite creates and drops an ephemeral database per session and covers:

* basic lifecycle, structured validation, immutability, ledger
  reconciliation after every step;
* the equality boundary (`expires_at == now()` ⇒ expired) and the
  one-microsecond-valid case, exercised deterministically inside a single
  database transaction (PostgreSQL freezes `now()` per transaction);
* real wall-clock expiry (5 s minimum lease) and quota reuse;
* 25-way concurrent reservation storm, concurrent duplicate confirms,
  confirm-vs-cancel races on the same token and across many tokens;
* a randomized 6-writer / 4-reader interleaving load asserting the
  conservation identity holds on **every** observed query.

## Layout

```
app/            FastAPI app, service core, pool/migrations, schemas
migrations/     Versioned SQL schema and immutability trigger
tests/          Real-PostgreSQL pytest suite
verify/         One-shot acceptance gate used by the compose service
```
