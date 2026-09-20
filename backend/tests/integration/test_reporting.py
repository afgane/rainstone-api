from decimal import Decimal
from pathlib import Path

from rainstone.db import engine
from rainstone.ingestion import ingest_fixture
from rainstone.models import CostRevision, IngestionEvent
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .conftest import headers


def test_three_recorded_batch_calculations(client) -> None:
    response = client.get("/api/jobs?basis=additional&limit=50", headers=headers("admin", True))
    assert response.status_code == 200
    amounts = {item["source_id"]: item["amount"] for item in response.json()["items"]}
    assert Decimal(amounts["13"]) == Decimal("0.006110803245")
    assert Decimal(amounts["16"]) == Decimal("0.002968758004")
    assert Decimal(amounts["18"]) == Decimal("0.002905946910")


def test_known_zero_and_unknown_are_distinct(client) -> None:
    items = client.get("/api/jobs?basis=additional", headers=headers("admin", True)).json()["items"]
    by_id = {item["source_id"]: item for item in items}
    assert by_id["9"]["amount"] == "0E-12"
    assert by_id["9"]["quality"] == "known_zero"
    assert by_id["21"]["amount"] is None
    assert by_id["21"]["quality"] == "partial"


def test_cross_runner_retry_is_one_job_with_attempt_costs_summed(client) -> None:
    items = client.get("/api/jobs?basis=additional", headers=headers("alice")).json()["items"]
    retried = next(item for item in items if item["source_id"] == "22")
    assert Decimal(retried["amount"]) == Decimal("0.001618633333")
    assert retried["attempt_cost_lines"] == 2
    assert retried["quality"] == "approximate"
    detail = client.get(f"/api/jobs/{retried['id']}?basis=additional", headers=headers("alice")).json()
    assert len(detail["cost"]["attempts"]) == 2
    assert {attempt["quality"] for attempt in detail["cost"]["attempts"]} == {
        "known_zero",
        "approximate",
    }


def test_mixed_and_nested_invocations_are_deduplicated(client) -> None:
    items = client.get("/api/invocations?basis=additional", headers=headers("alice")).json()
    by_source = {item["source_id"]: item for item in items}
    assert Decimal(by_source["invocation-mixed"]["amount"]) == Decimal("0.006110803245")
    assert by_source["invocation-mixed"]["job_count"] == 5
    assert by_source["invocation-nested-root"]["job_count"] == 2
    assert by_source["invocation-nested-child"]["job_count"] == 2


def test_owner_scope_and_admin_scope_do_not_leak(client) -> None:
    alice = client.get("/api/jobs", headers=headers("alice"))
    assert {item["source_id"] for item in alice.json()["items"]}.isdisjoint({"18", "21"})
    denied = client.get("/api/jobs", headers=headers("alice", True))
    assert denied.status_code == 403
    bob = client.get("/api/jobs", headers=headers("bob"))
    assert {item["source_id"] for item in bob.json()["items"]} == {"18", "21"}


def test_replay_is_idempotent_and_preserves_totals(client) -> None:
    before = client.get("/api/summary", headers=headers("admin", True)).json()
    with Session(engine) as session:
        revision_count = session.scalar(select(func.count()).select_from(CostRevision))
        result = ingest_fixture(session, Path("fixtures/phase1.json"))
        assert result["replayed"] is True
        assert session.scalar(select(func.count()).select_from(IngestionEvent)) == 1
        assert session.scalar(select(func.count()).select_from(CostRevision)) == revision_count
    after = client.get("/api/summary", headers=headers("admin", True)).json()
    assert after == before
