"""Reports separate what ran, where the evidence came from, and when it ran.

Each test builds its own tenant in the shape the AnVIL dev snapshot showed:
Galaxy and the provider both describe one Batch execution, finished local work
arrives with timing but no placement, and zero-cost work on the existing server
can last no measurable time at all.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from rainstone.adapters.contracts import (
    NormalizedAttempt,
    NormalizedJob,
    NormalizedLifetime,
    NormalizedOwner,
    ObservationBatch,
)
from rainstone.baseline import BaselineProfile
from rainstone.config import Settings
from rainstone.costing import calculate_tenant
from rainstone.db import engine
from rainstone.doctor import _baseline_coverage
from rainstone.ingestion import apply_batch, reclassify_baseline, stable_id
from rainstone.models import CapacityRelationship, CostLine, ExecutionAttempt, Job, Tenant
from sqlalchemy import select
from sqlalchemy.orm import Session

DAY = datetime(2026, 9, 23, tzinfo=UTC)
USER = "researcher"


def _lifetime(key: str, start: datetime, end: datetime, relationship=CapacityRelationship.dedicated):
    existing = relationship == CapacityRelationship.existing
    return NormalizedLifetime(
        provider="gcp",
        resource_key=key,
        resource_uid=key,
        capacity_relationship=relationship,
        timing_method="configured_baseline_occupancy" if existing else "compute_instance_timestamps",
        region="us-central1",
        machine_type="n2-highmem-4",
        purchase_model="on_demand",
        observed_start=start,
        observed_end=end,
    )


def _job(source_id: str, *, state="ok", runner="gcp_batch", destination=None, attempts=()):
    return NormalizedJob(
        source_id=source_id,
        owner_source_id=USER,
        tool_id=f"tool-{source_id}",
        state=state,
        created_at=DAY,
        updated_at=DAY,
        runner=runner,
        destination=destination,
        attempts=tuple(attempts),
    )


def _attempt(source_id: str, attempt_id: str, *, start=None, end=None, lifetimes=(), task=None,
             ordinal=None, outcome="ok"):
    return NormalizedAttempt(
        job_source_id=source_id,
        source_attempt_id=attempt_id,
        runner="gcp_batch",
        outcome=outcome,
        task_index=task,
        attempt_ordinal=ordinal,
        tool_started_at=start,
        tool_finished_at=end,
        lifetimes=tuple(lifetimes),
    )


def _galaxy(source_id: str, start=None, end=None, lifetimes=()):
    return _attempt(source_id, "galaxy-0", start=start, end=end, lifetimes=lifetimes)


@pytest.fixture()
def tenant():
    slug = f"evidence-{uuid.uuid4().hex[:8]}"
    tenant_id = stable_id("tenant", slug)
    instant = datetime(2026, 9, 21, 12, 48, 33, tzinfo=UTC)
    dual_start = DAY + timedelta(hours=1)
    first_start = DAY + timedelta(hours=2)
    retry_start = first_start + timedelta(minutes=30)
    jobs = (
        # Zero-cost work on the existing server, over in the same instant.
        _job("instant", runner="local", attempts=[_galaxy(
            "instant", instant, instant,
            [_lifetime(f"baseline:{slug}:instant", instant, instant, CapacityRelationship.existing)],
        )]),
        # Galaxy's record and the provider's describe the same execution.
        _job("dual", attempts=[
            _galaxy("dual", dual_start, dual_start + timedelta(minutes=10)),
            _attempt("dual", "batch:dual:task-0:attempt-0", start=dual_start,
                     end=dual_start + timedelta(minutes=10), task=0, ordinal=0,
                     lifetimes=[_lifetime(f"{slug}-dual", dual_start, dual_start + timedelta(minutes=10))]),
        ]),
        # A genuine second attempt on a VM of its own.
        _job("retried", attempts=[
            _galaxy("retried"),
            _attempt("retried", "batch:retried:task-0:attempt-0", start=first_start,
                     end=first_start + timedelta(minutes=10), task=0, ordinal=0, outcome="failed",
                     lifetimes=[_lifetime(f"{slug}-first", first_start, first_start + timedelta(minutes=10))]),
            _attempt("retried", "batch:retried:task-0:attempt-1", start=retry_start,
                     end=retry_start + timedelta(minutes=20), task=0, ordinal=1,
                     lifetimes=[_lifetime(f"{slug}-retry", retry_start, retry_start + timedelta(minutes=20))]),
        ]),
        # Finished, with Galaxy's timing but no evidence of where it ran.
        _job("unplaced", runner="local", destination="local", attempts=[
            _galaxy("unplaced", DAY + timedelta(hours=3), DAY + timedelta(hours=3, minutes=1)),
        ]),
        _job("paused", state="paused", runner=None),
    )
    with Session(engine) as session:
        session.add(Tenant(
            id=tenant_id, slug=slug, display_name="evidence",
            capabilities={"demo": False, "imported_snapshot": {
                "captured_at": "2026-09-23T02:40:16Z", "snapshot_digest": "abc", "ignored": "x",
            }},
        ))
        session.flush()
        apply_batch(session, tenant_id, ObservationBatch(
            source="galaxy_db", observed_at=DAY,
            owners=(NormalizedOwner(source_id=USER, label="Researcher"),), jobs=jobs,
        ))
        calculate_tenant(session, tenant_id, reason="test")
        session.commit()
    yield {"slug": slug, "id": tenant_id}
    with Session(engine) as session:
        session.delete(session.get(Tenant, tenant_id))
        session.commit()


def _get(client, tenant, path):
    response = client.get(
        f"/api/{path}", headers={"X-Rainstone-Tenant": tenant["slug"], "X-Rainstone-User": USER}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _day(date: datetime) -> str:
    start = date.replace(hour=0, minute=0, second=0)
    return f"from={start.isoformat()}&to={(start + timedelta(days=1)).isoformat()}".replace("+", "%2B")


def test_undated_work_is_reported_beside_a_period_never_in_it(client, tenant) -> None:
    today = _get(client, tenant, f"summary?{_day(DAY + timedelta(days=1))}")
    assert today["job_count"] == 0
    assert today["amount"] is None
    # Unplaceable evidence stays visible, just outside every period.
    assert today["undated"]["job_count"] == 2
    jobs = _get(client, tenant, f"jobs?{_day(DAY + timedelta(days=1))}")
    assert jobs["items"] == []
    assert {item["source_id"] for item in jobs["undated_items"]} == {"unplaced", "paused"}
    runs = _get(client, tenant, f"invocations?{_day(DAY + timedelta(days=1))}")
    assert runs["items"] == []

    whole = _get(client, tenant, "summary")
    assert whole["job_count"] == 5
    assert whole["undated"] is None


def test_an_instant_of_known_zero_work_belongs_to_its_own_day(client, tenant) -> None:
    that_day = _get(client, tenant, f"jobs?{_day(datetime(2026, 9, 21, tzinfo=UTC))}")
    assert [(item["source_id"], item["amount"], item["quality"]) for item in that_day["items"]] == [
        ("instant", "0E-12", "known_zero"),
    ]
    daily = _get(client, tenant, f"daily?{_day(datetime(2026, 9, 21, tzinfo=UTC))}")
    assert [(item["date"], item["job_count"]) for item in daily["items"]] == [("2026-09-21", 1)]
    next_day = _get(client, tenant, f"jobs?{_day(datetime(2026, 9, 22, tzinfo=UTC))}")
    assert next_day["items"] == []


def test_two_views_of_one_execution_are_not_a_retry(client, tenant) -> None:
    jobs = {item["source_id"]: item for item in _get(client, tenant, "jobs")["items"]}
    assert jobs["dual"]["attempt_count"] == 1
    assert jobs["dual"]["repeat_attempt_count"] == 0
    assert jobs["dual"]["observation_count"] == 2
    assert jobs["dual"]["attempt_evidence"] == "provider"
    assert jobs["retried"]["attempt_count"] == 2
    assert jobs["retried"]["repeat_attempt_count"] == 1
    assert jobs["unplaced"]["attempt_evidence"] == "galaxy_record"

    detail = _get(client, tenant, f"jobs/{jobs['dual']['id']}")
    assert sorted(attempt["role"] for attempt in detail["attempts"]) == ["first", "observation"]


def test_repeat_spend_is_only_what_the_repeats_used(client, tenant) -> None:
    summary = _get(client, tenant, "summary")
    with Session(engine) as session:
        retried = session.scalar(
            select(Job).where(Job.tenant_id == tenant["id"], Job.source_id == "retried")
        )
        lines = session.scalars(
            select(CostLine).where(CostLine.job_id == retried.id, CostLine.basis == "additional")
        ).all()
        by_key = {line.details["resource_key"]: line.amount for line in lines}
    repeat = by_key[f"{tenant['slug']}-retry"]
    assert summary["repeated_job_count"] == 1
    assert Decimal(summary["repeated_job_spend"]) == sum(by_key.values())
    assert Decimal(summary["repeat_attempt_spend"]) == repeat
    assert summary["repeat_attempt_spend_complete"] is True
    # Costs are unchanged by reconciliation: every priced line is counted once.
    everything = _get(client, tenant, "jobs")["items"]
    assert Decimal(summary["amount"]) == sum(
        Decimal(item["amount"]) for item in everything if item["amount"] is not None
    )


def test_missing_evidence_is_not_reported_as_running(client, tenant) -> None:
    jobs = {item["source_id"]: item for item in _get(client, tenant, "jobs")["items"]}
    assert jobs["unplaced"]["quality"] == "unavailable"
    assert "Recalculating alone will not recover it" in jobs["unplaced"]["reason"]
    assert jobs["paused"]["quality"] == "not_started"


def test_an_imported_snapshot_is_marked_rather_than_called_a_demo(client, tenant) -> None:
    summary = _get(client, tenant, "summary")
    assert summary["demo"] is False
    assert summary["imported_snapshot"] == {
        "captured_at": "2026-09-23T02:40:16Z", "snapshot_digest": "abc",
        "source_cutoffs": None, "label": None,
    }


def test_reclassification_follows_the_corrected_profile(tenant) -> None:
    profile = BaselineProfile(
        version="v", resource_uid="host", destinations=("local",), runners=("local",)
    )
    with Session(engine) as session:
        # Work earlier classified as baseline only because of its runner.
        stale = session.scalar(
            select(Job).where(Job.tenant_id == tenant["id"], Job.source_id == "instant")
        )
        stale.destination = "k8s"
        session.flush()
        counts = reclassify_baseline(session, tenant["id"], profile)
        assert (counts["added"], counts["removed"]) == (1, 1)
        # Provider evidence already places the Batch jobs.
        assert counts["placed_by_provider"] == 2
        revision = calculate_tenant(session, tenant["id"], reason="test")
        session.commit()
        lines = {
            line.job_id: line for line in session.scalars(
                select(CostLine).where(
                    CostLine.revision_id == revision.id, CostLine.basis == "additional"
                )
            )
        }
        unplaced = session.scalar(
            select(Job).where(Job.tenant_id == tenant["id"], Job.source_id == "unplaced")
        )
        assert lines[unplaced.id].quality.value == "known_zero"
        assert stale.id not in lines
        again = reclassify_baseline(session, tenant["id"], profile)
        assert (again["added"], again["removed"]) == (0, 0)
        session.rollback()


def test_the_doctor_names_placements_the_profile_misses(tenant) -> None:
    settings = Settings(
        auth_mode="development", demo_data=True, tenant_slug=tenant["slug"],
        baseline_policy_version="v", baseline_resource_uid="host", baseline_runners="local",
    )
    with Session(engine) as session:
        [check] = _baseline_coverage(session, settings)
    assert check.status == "warn"
    assert "local (1 jobs)" in check.detail
    covered = settings.model_copy(update={"baseline_destinations": "local"})
    with Session(engine) as session:
        [check] = _baseline_coverage(session, covered)
    assert check.status == "pass"



def test_ordinary_collection_respects_the_saved_policy_period(tenant) -> None:
    """Collection, reclassification and recollection all apply one policy period."""
    from rainstone.collector import baseline_transform, resolve_baseline_profile
    from rainstone.doctor import _baseline_period
    from rainstone.models import LifetimeAttempt

    declared = Settings(
        auth_mode="development", demo_data=True, tenant_slug=tenant["slug"],
        baseline_policy_version="bounded", baseline_resource_uid="host",
        baseline_runners="local", baseline_effective_from=datetime(2026, 9, 22, tzinfo=UTC),
    )
    early = datetime(2026, 9, 21, 9, tzinfo=UTC)
    inside = datetime(2026, 9, 23, 9, tzinfo=UTC)
    batch = ObservationBatch(source="galaxy_db", observed_at=DAY, jobs=(
        _job("early", runner="local", attempts=[_galaxy("early", early, early + timedelta(minutes=1))]),
        _job("inside", runner="local", attempts=[_galaxy("inside", inside, inside + timedelta(minutes=1))]),
    ))

    def classified(session) -> set[str]:
        return set(session.scalars(
            select(Job.source_id)
            .join(ExecutionAttempt, ExecutionAttempt.job_id == Job.id)
            .join(LifetimeAttempt, LifetimeAttempt.attempt_id == ExecutionAttempt.id)
            .where(Job.tenant_id == tenant["id"], Job.source_id.in_(("early", "inside")))
        ))

    with Session(engine) as session:
        profile = resolve_baseline_profile(session, tenant["id"], declared)
        apply_batch(session, tenant["id"], baseline_transform(profile)(batch))
        session.commit()
        assert classified(session) == {"inside"}

        reclassify_baseline(session, tenant["id"], resolve_baseline_profile(session, tenant["id"], declared))
        session.commit()
        assert classified(session) == {"inside"}

        # A restart whose configuration no longer declares the period keeps the saved one.
        undeclared = declared.model_copy(update={"baseline_effective_from": None})
        profile = resolve_baseline_profile(session, tenant["id"], undeclared)
        assert profile.effective_from == declared.baseline_effective_from
        apply_batch(session, tenant["id"], baseline_transform(profile)(batch))
        session.commit()
        assert classified(session) == {"inside"}

        # A different period under the same version classifies nothing and fails the check.
        moved = declared.model_copy(update={"baseline_effective_from": early})
        assert resolve_baseline_profile(session, tenant["id"], moved) is None
        [check] = _baseline_period(session, moved)
        assert check.status == "fail"
        [check] = _baseline_period(session, undeclared)
        assert check.status == "pass"


def test_a_repeat_without_resource_evidence_leaves_its_cost_unknown(client, tenant) -> None:
    start = DAY + timedelta(hours=5)
    retry = start + timedelta(minutes=30)
    with Session(engine) as session:
        apply_batch(session, tenant["id"], ObservationBatch(source="gcp_batch", observed_at=DAY, jobs=(
            _job("uncosted", attempts=[
                _attempt("uncosted", "batch:uncosted:task-0:attempt-0", start=start,
                         end=start + timedelta(minutes=10), task=0, ordinal=0, outcome="failed",
                         lifetimes=[_lifetime(f"{tenant['slug']}-uncosted", start, start + timedelta(minutes=10))]),
                # A second attempt ran, but nothing says what it ran on.
                _attempt("uncosted", "batch:uncosted:task-0:attempt-1", start=retry,
                         end=retry + timedelta(minutes=10), task=0, ordinal=1),
            ]),
        )))
        calculate_tenant(session, tenant["id"], reason="test")
        session.commit()
    summary = _get(client, tenant, "summary")
    assert summary["repeated_job_count"] == 2
    assert summary["repeat_attempt_spend_complete"] is False
    jobs = {item["source_id"]: item for item in _get(client, tenant, "jobs")["items"]}
    assert jobs["uncosted"]["quality"] == "partial"
    assert "no resource evidence" in jobs["uncosted"]["reason"]
    # A repeat outside the period does not make that period's subtotal incomplete.
    window = f"from={start.isoformat()}&to={(start + timedelta(minutes=20)).isoformat()}"
    first_only = _get(client, tenant, "summary?" + window.replace("+", "%2B"))
    assert first_only["repeated_job_count"] == 1
    assert first_only["repeat_attempt_spend_complete"] is True


def test_the_undated_list_pages_on_its_own(client, tenant) -> None:
    query = f"jobs?{_day(DAY + timedelta(days=1))}&limit=1"
    first = _get(client, tenant, query)["undated_items"]
    second = _get(client, tenant, f"{query}&undated_offset=1")["undated_items"]
    assert len(first) == len(second) == 1
    assert {first[0]["source_id"], second[0]["source_id"]} == {"unplaced", "paused"}
