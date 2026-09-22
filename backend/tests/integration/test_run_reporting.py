"""Workflow runs answer a different question from period spending."""

from decimal import Decimal

from .conftest import headers


def test_a_run_reports_its_whole_cost_beside_the_period_share(client) -> None:
    auth = headers("alice")
    # The midnight-crossing fixture run accrues cost on both sides of midnight.
    whole = client.get("/api/invocations", headers=auth).json()["items"]
    mixed = next(item for item in whole if item["source_id"] == "invocation-mixed")
    assert mixed["run_total"] is not None

    clipped = client.get(
        "/api/invocations?from=2026-09-19T13:29:00Z&to=2026-09-19T13:30:00Z", headers=auth
    ).json()["items"]
    scoped = next(item for item in clipped if item["source_id"] == "invocation-mixed")
    # The run total is the same run; only the period share changes.
    assert scoped["run_total"] == mixed["run_total"]
    assert scoped["amount"] != scoped["run_total"]
    assert Decimal(scoped["amount"]) < Decimal(scoped["run_total"])


def test_runs_are_listed_by_overlap_with_the_selected_period(client) -> None:
    auth = headers("alice")
    outside = client.get(
        "/api/invocations?from=2020-01-01T00:00:00Z&to=2020-01-02T00:00:00Z", headers=auth
    ).json()
    assert outside["items"] == []
    inside = client.get(
        "/api/invocations?from=2026-09-19T00:00:00Z&to=2026-09-21T00:00:00Z", headers=auth
    ).json()
    assert {item["source_id"] for item in inside["items"]} >= {"invocation-mixed"}


def test_a_run_carries_a_status_derived_from_its_executions(client) -> None:
    auth = headers("alice")
    items = client.get("/api/invocations", headers=auth).json()["items"]
    statuses = {item["source_id"]: item["run_status"] for item in items}
    assert set(statuses.values()) <= {
        "completed", "failed", "running", "cancelled", "no runs recorded",
    }
    mixed = next(item for item in items if item["source_id"] == "invocation-mixed")
    assert mixed["started_at"] is not None
    assert mixed["run_job_count"] >= mixed["job_count"]


def test_tool_identities_carry_a_readable_name(client) -> None:
    auth = headers("alice")
    tools = client.get("/api/tools", headers=auth).json()["items"]
    goseq = next(item for item in tools if item["tool_id"].endswith("goseq/2.0.1"))
    assert goseq["tool_name"] == "goseq"
    # The full identity stays available for grouping and provenance.
    assert goseq["tool_id"].startswith("toolshed.g2.bx.psu.edu/")
    jobs = client.get("/api/jobs", headers=auth).json()["items"]
    assert all(item["tool_name"] for item in jobs)
