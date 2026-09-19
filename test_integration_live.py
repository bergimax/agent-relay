"""Integration test against a *running* relay process and its real database.

Unlike test_agent_relay.py (which drives an in-process TestClient against a
throwaway scratch DB), this test speaks real HTTP to a server the operator
already started, and then reads the result back out of that server's own
database -- SQLite or PostgreSQL, whichever it's actually configured with. It
exercises SPEC.md acceptance scenario 1: "Register two agents. One sends a
task; the other claims and completes it; the sender reads the result."

Requires a live server (e.g. `uv run uvicorn main:app --reload`, or the
`docker compose` stack) reachable at RELAY_TEST_BASE_URL (default
http://127.0.0.1:8000), and a SQLAlchemy URL for that same server's database
at RELAY_TEST_DB_URL (default sqlite:///./agent-relay.db, matching
database.py's own default; point it at
postgresql+psycopg://user:pass@host:port/db to verify a Postgres-backed
server). Skips automatically if the server isn't reachable.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, text

BASE_URL = os.getenv("RELAY_TEST_BASE_URL", "http://127.0.0.1:8000")
DB_URL = os.getenv("RELAY_TEST_DB_URL", "sqlite:///./agent-relay.db")


def _server_reachable() -> bool:
    try:
        return httpx.get(f"{BASE_URL}/health", timeout=5.0).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _server_reachable(),
    reason=f"no live relay server reachable at {BASE_URL}",
)


def register(client: httpx.Client, name: str) -> tuple[str, str]:
    response = client.post("/api/v1/agents", json={"name": name})
    assert response.status_code == 201, response.text
    body = response.json()
    return body["agent_id"], body["token"]


def test_two_agents_exchange_a_task_and_its_result_via_live_api_and_db():
    run_id = uuid.uuid4().hex[:8]
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        sender_id, sender_token = register(client, f"live-sender-{run_id}")
        recipient_id, recipient_token = register(client, f"live-recipient-{run_id}")
        sender_headers = {"Authorization": f"Bearer {sender_token}"}
        recipient_headers = {"Authorization": f"Bearer {recipient_token}"}

        task_input = f"live integration payload {run_id}"
        sent = client.post(
            "/api/v1/tasks",
            headers=sender_headers,
            json={"to": recipient_id, "input": task_input},
        )
        assert sent.status_code == 201, sent.text
        task_id = sent.json()["task_id"]
        assert sent.json()["status"] == "queued"

        claim = client.post(
            "/api/v1/tasks/claim",
            headers=recipient_headers,
            json={"worker_id": f"live-worker-{run_id}", "wait_seconds": 5},
        )
        assert claim.status_code == 200, claim.text
        claim_data = claim.json()
        assert claim_data["task_id"] == task_id
        assert claim_data["input"] == task_input

        expected_output = task_input.upper()
        complete = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=recipient_headers,
            json={"claim_token": claim_data["claim_token"], "output": expected_output},
        )
        assert complete.status_code == 200, complete.text
        assert complete.json()["status"] == "completed"

        # The sender reads the result back through the real API.
        result = client.get(f"/api/v1/tasks/{task_id}", headers=sender_headers)
        assert result.status_code == 200, result.text
        result_body = result.json()
        assert result_body["status"] == "completed"
        assert result_body["output"] == expected_output
        assert result_body["error"] is None
        assert result_body["from"] == sender_id
        assert result_body["to"] == recipient_id

    # And independently, the same result is durably persisted in the
    # server's real database -- not just echoed back over HTTP.
    engine = create_engine(DB_URL)
    try:
        with engine.connect() as con:
            row = con.execute(
                text("SELECT status, output, sender_id, recipient_id FROM tasks WHERE id = :id"),
                {"id": task_id},
            ).one_or_none()
            assert row is not None, f"task {task_id} not found in {DB_URL}"
            db_status, db_output, db_sender_id, db_recipient_id = row
            assert db_status == "completed"
            assert db_output == expected_output
            assert db_sender_id == sender_id
            assert db_recipient_id == recipient_id

            attempt = con.execute(
                text(
                    "SELECT outcome, worker_id FROM attempts WHERE task_id = :id "
                    "ORDER BY attempt_number DESC LIMIT 1"
                ),
                {"id": task_id},
            ).one_or_none()
            assert attempt is not None
            db_outcome, db_worker_id = attempt
            assert db_outcome == "completed"
            assert db_worker_id == f"live-worker-{run_id}"
    finally:
        engine.dispose()
