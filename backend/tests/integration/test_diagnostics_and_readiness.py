"""Diagnostics reported by the process that can run them, and readiness gating."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from rainstone.config import Settings, get_settings
from rainstone.db import engine
from rainstone.doctor import readiness, record_report, run_checks
from rainstone.ingestion import stable_id
from rainstone.main import app
from rainstone.models import CapabilityReport
from sqlalchemy.orm import Session

TENANT_ID = stable_id("tenant", "anvil-demo")
WORKSPACE = Settings(
    auth_mode="anvil-workspace",
    tenant_slug="anvil-demo",
    workspace_owner_source_id="alice",
    demo_data=False,
)


def collector_report(generated_at: datetime, status: str = "pass") -> dict:
    return {
        "generated_at": generated_at.isoformat(),
        "context": "collector",
        "overall_status": status,
        "auth_mode": "anvil-workspace",
        "tenant": "anvil-demo",
        "checks": [
            {
                "name": "gcp:batch.jobs.list",
                "status": status,
                "detail": "batch.jobs.list is permitted.",
                "facts": {"probe_result": "ok"},
            }
        ],
    }


@pytest.fixture()
def recorded():
    def record(generated_at: datetime, status: str = "pass") -> None:
        with Session(engine) as session:
            record_report(session, TENANT_ID, collector_report(generated_at, status))
            session.commit()

    yield record
    with Session(engine) as session:
        for row in session.query(CapabilityReport).filter(
            CapabilityReport.tenant_id == TENANT_ID
        ):
            session.delete(row)
        session.commit()


@pytest.fixture()
def workspace_client(client):
    app.dependency_overrides[get_settings] = lambda: WORKSPACE
    yield client
    app.dependency_overrides.pop(get_settings, None)


def test_the_web_context_does_not_pretend_to_probe_source_or_cloud() -> None:
    with Session(engine) as session:
        report = run_checks(session, WORKSPACE, context="web")
    names = {check["name"] for check in report["checks"]}
    assert "galaxy_source" not in names
    assert not any(name.startswith("gcp:") for name in names)
    assert "collector_diagnostics" in names


def test_recorded_collector_findings_appear_with_their_own_timestamp(recorded) -> None:
    recorded(datetime.now(UTC))
    with Session(engine) as session:
        report = run_checks(session, WORKSPACE, context="web")
    entry = next(
        check for check in report["checks"] if check["name"] == "collector:gcp:batch.jobs.list"
    )
    assert entry["status"] == "pass"
    assert entry["facts"]["recorded_by"] == "collector"
    assert report["recorded_reports"][0]["stale"] is False


def test_stale_collector_findings_never_read_as_current_successes(recorded) -> None:
    recorded(datetime.now(UTC) - timedelta(hours=2))
    with Session(engine) as session:
        report = run_checks(session, WORKSPACE, context="web")
    entry = next(
        check for check in report["checks"] if check["name"] == "collector:gcp:batch.jobs.list"
    )
    assert entry["status"] == "warn"
    assert "has not reported since" in entry["detail"]
    assert report["recorded_reports"][0]["stale"] is True
    assert report["overall_status"] in {"warn", "fail"}


def test_denied_cloud_access_reaches_the_status_page(recorded, workspace_client) -> None:
    with Session(engine) as session:
        record_report(
            session,
            TENANT_ID,
            {
                **collector_report(datetime.now(UTC)),
                "overall_status": "fail",
                "checks": [
                    {
                        "name": "gcp:compute.instances.list",
                        "status": "fail",
                        "detail": "compute.instances.list is denied for this deployment identity.",
                        "facts": {"probe_result": "denied"},
                    }
                ],
            },
        )
        session.commit()
    payload = workspace_client.get("/api/status").json()
    denied = next(
        check
        for check in payload["checks"]
        if check["name"] == "collector:gcp:compute.instances.list"
    )
    assert denied["status"] == "fail"
    assert denied["facts"]["probe_result"] == "denied"
    assert payload["overall_status"] == "fail"


def test_readiness_requires_migrations_and_a_resolved_scope() -> None:
    with Session(engine) as session:
        state = readiness(session, WORKSPACE)
        assert state["ready"] is True
        assert state["facts"]["applied_schema"] == state["facts"]["expected_schema"]

        unresolved = readiness(
            session, WORKSPACE.model_copy(update={"workspace_owner_source_id": "nobody"})
        )
        assert unresolved["ready"] is False
        assert any("resolves to 0 owners" in reason for reason in unresolved["reasons"])

        unseeded = readiness(
            session, WORKSPACE.model_copy(update={"tenant_slug": f"missing-{uuid.uuid4().hex[:6]}"})
        )
        assert unseeded["ready"] is False
        assert any("not seeded" in reason for reason in unseeded["reasons"])


def test_the_readiness_endpoint_gates_traffic_but_health_does_not(workspace_client) -> None:
    assert workspace_client.get("/api/health").status_code == 200
    assert workspace_client.get("/api/ready").status_code == 200

    app.dependency_overrides[get_settings] = lambda: WORKSPACE.model_copy(
        update={"workspace_owner_source_id": "nobody"}
    )
    blocked = workspace_client.get("/api/ready")
    assert blocked.status_code == 503
    assert blocked.json()["reasons"]
    # Liveness stays independent of readiness, so a pod is not restarted.
    assert workspace_client.get("/api/health").status_code == 200


def test_history_from_installation_does_not_decide_current_health(recorded) -> None:
    """A one-time installation finding must not outlive its recovery."""
    with Session(engine) as session:
        record_report(
            session,
            TENANT_ID,
            {
                "generated_at": (datetime.now(UTC) - timedelta(days=3)).isoformat(),
                "context": "installation",
                "overall_status": "fail",
                "auth_mode": "anvil-workspace",
                "tenant": "anvil-demo",
                "checks": [
                    {
                        "name": "gcp:batch.jobs.list",
                        "status": "fail",
                        "detail": "batch.jobs.list is denied for this deployment identity.",
                        "facts": {},
                    },
                    {
                        "name": "source_evidence",
                        "status": "pass",
                        "detail": "Source evidence: the fingerprint is readable.",
                        "facts": {},
                    },
                ],
            },
        )
        session.commit()
    recorded(datetime.now(UTC))  # the collector has since reported success

    with Session(engine) as session:
        report = run_checks(session, WORKSPACE, context="web")
    names = {check["name"] for check in report["checks"]}
    # A current check supersedes the installation-time finding for the same
    # capability, and history never raises the overall status.
    assert "collector:gcp:batch.jobs.list" in names
    assert "installation:gcp:batch.jobs.list" not in names
    assert "installation:source_evidence" in names
    # The installation-time failure no longer decides current health.
    assert report["overall_status"] != "fail"
    assert not [
        check
        for check in report["checks"]
        if check["status"] == "fail" and not check.get("history")
    ]
    history = next(
        check for check in report["checks"] if check["name"] == "installation:source_evidence"
    )
    assert history["history"] is True
    assert "during installation" in history["detail"]
    kinds = {source["context"]: source["kind"] for source in report["recorded_reports"]}
    assert kinds == {"collector": "current", "installation": "history"}


def test_readiness_waits_for_this_release_initialization() -> None:
    from rainstone.doctor import mark_installed

    release = f"anvil-{uuid.uuid4().hex[:6]}"
    settings = WORKSPACE.model_copy(update={"installation_id": release})
    with Session(engine) as session:
        pending = readiness(session, settings)
        assert pending["ready"] is False
        assert any(release in reason for reason in pending["reasons"])

        mark_installed(session, settings, {"source": "test"})
        session.commit()
        assert readiness(session, settings)["ready"] is True
        # A later release is not covered by an earlier marker.
        later = settings.model_copy(update={"installation_id": f"{release}-2"})
        assert readiness(session, later)["ready"] is False
