"""Shared fixtures for the black-box acceptance run (HTTP only, no app imports)."""
from __future__ import annotations

import os
import time

import httpx
import pytest


def _base_url() -> str:
    return os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")


@pytest.fixture(scope="session")
def base_url() -> str:
    url = _base_url()
    deadline = time.time() + 60
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=2)
            if r.status_code == 200:
                return url
        except Exception as exc:  # noqa: BLE001 - startup race
            last = exc
        time.sleep(1)
    raise RuntimeError(f"API at {url} never became healthy: {last}")


@pytest.fixture()
def client(base_url):
    with httpx.Client(base_url=base_url, timeout=10) as c:
        yield c


@pytest.fixture()
def make_batch(client):
    def _make(total_ml: int) -> int:
        r = client.post("/batches", json={"total_ml": total_ml})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    return _make


def assert_conserved(batch: dict) -> dict:
    assert batch["total_ml"] == (
        batch["available_ml"] + batch["reserved_ml"] + batch["confirmed_ml"]
    ), batch
    for key in ("available_ml", "reserved_ml", "confirmed_ml"):
        assert batch[key] >= 0, batch
    return batch


@pytest.fixture()
def get_balance(client):
    def _get(batch_id: int) -> dict:
        r = client.get(f"/batches/{batch_id}")
        assert r.status_code == 200, r.text
        return assert_conserved(r.json())

    return _get
