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
    items = client.get("/api/invocations?basis=additional", headers=headers("alice")).json()["items"]
    by_source = {item["source_id"]: item for item in items}
    assert Decimal(by_source["invocation-mixed"]["amount"]) == Decimal("0.006110803245")
    assert by_source["invocation-mixed"]["job_count"] == 5
    assert by_source["invocation-nested-root"]["job_count"] == 2
    root = by_source["invocation-nested-root"]
    detail = client.get(
        f"/api/invocations/{root['id']}?basis=additional", headers=headers("alice")
    ).json()
    assert detail["children"][0]["source_id"] == "invocation-nested-child"
    assert detail["children"][0]["job_count"] == 2


def test_owner_scope_and_admin_scope_do_not_leak(client) -> None:
    alice = client.get("/api/jobs", headers=headers("alice"))
    assert {item["source_id"] for item in alice.json()["items"]}.isdisjoint({"18", "21"})
    denied = client.get("/api/jobs", headers=headers("alice", True))
    assert denied.status_code == 403
    bob = client.get("/api/jobs", headers=headers("bob"))
    assert {item["source_id"] for item in bob.json()["items"]} == {"18", "21", "25"}


def test_combined_filters_reconcile_summary_table_daily_and_export(client) -> None:
    query = "basis=additional&runner=gcp_batch&search=fastqc"
    report = client.get(f"/api/summary?{query}", headers=headers("admin", True)).json()
    jobs = client.get(f"/api/jobs?{query}", headers=headers("admin", True)).json()
    daily = client.get(f"/api/daily?{query}", headers=headers("admin", True)).json()
    exported = client.get(f"/api/export/jobs.csv?{query}", headers=headers("admin", True))
    assert report["job_count"] == jobs["total"] == 2
    assert sum(Decimal(item["amount"]) for item in jobs["items"]) == Decimal(report["amount"])
    assert sum(Decimal(item["amount"]) for item in daily["items"]) == Decimal(report["amount"])
    assert exported.status_code == 200
    assert exported.text.count("\n") == 3
    assert report["revision_id"] == jobs["meta"]["revision_id"] == daily["meta"]["revision_id"]


def test_permissions_unknown_zero_and_query_validation(client) -> None:
    assert client.get("/api/users", headers=headers("alice")).status_code == 403
    assert client.get("/api/infrastructure", headers=headers("alice")).status_code == 403
    assert client.get("/api/summary?currency=EUR", headers=headers("alice")).status_code == 422
    bob = client.get("/api/jobs", headers=headers("bob")).json()["items"]
    by_source = {item["source_id"]: item for item in bob}
    assert by_source["21"]["amount"] is None
    assert by_source["25"]["quality"] == "in_progress"
    exported = client.get("/api/export/jobs.csv", headers=headers("bob")).text
    assert "Alice Researcher" not in exported


def test_daily_boundaries_price_change_and_completed_mode(client) -> None:
    auth = headers("alice")
    jobs = client.get("/api/jobs?search=midnight-price", headers=auth).json()["items"]
    assert len(jobs) == 1
    assert Decimal(jobs[0]["amount"]) == Decimal("0.003285300000")
    daily = client.get("/api/daily?search=midnight-price&timezone=UTC", headers=auth).json()
    assert [item["date"] for item in daily["items"]] == ["2026-09-19", "2026-09-20"]
    assert sum(Decimal(item["amount"]) for item in daily["items"]) == Decimal(jobs[0]["amount"])
    dst = client.get(
        "/api/daily?search=time-boundary&timezone=America%2FNew_York", headers=auth
    ).json()
    assert [item["date"] for item in dst["items"]] == ["2026-03-08"]
    completed = client.get(
        "/api/summary?mode=completed&from=2026-09-20T00:00:00Z&to=2026-09-21T00:00:00Z",
        headers=auth,
    ).json()
    assert completed["job_count"] == 1


def test_workflow_uses_stable_identity(client) -> None:
    items = client.get("/api/invocations", headers=headers("alice")).json()["items"]
    mixed = next(item for item in items if item["source_id"] == "invocation-mixed")
    assert mixed["workflow_id"] == "workflow-rnaseq-mixed"


def test_replay_is_idempotent_and_preserves_totals(client) -> None:
    before = client.get("/api/summary", headers=headers("admin", True)).json()
    with Session(engine) as session:
        revision_count = session.scalar(select(func.count()).select_from(CostRevision))
        event_count = session.scalar(select(func.count()).select_from(IngestionEvent))
        result = ingest_fixture(session, Path("fixtures/phase1.json"))
        assert result["replayed"] is True
        assert session.scalar(select(func.count()).select_from(IngestionEvent)) == event_count
        assert session.scalar(select(func.count()).select_from(CostRevision)) == revision_count
    after = client.get("/api/summary", headers=headers("admin", True)).json()
    assert after == before
