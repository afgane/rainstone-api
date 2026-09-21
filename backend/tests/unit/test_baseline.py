from datetime import UTC, datetime

from rainstone.adapters.contracts import NormalizedAttempt, NormalizedJob
from rainstone.baseline import BaselineProfile, classify_job
from rainstone.models import CapacityRelationship

PROFILE = BaselineProfile(
    version="anvil-baseline-v1",
    resource_uid="galaxy-baseline-vm",
    machine_type="t2d-standard-4",
    region="us-central1",
    zone="us-central1-a",
    destinations=("local", "k8s"),
    runners=("local",),
)


def job(
    *, runner: str = "local", destination: str | None = "local", started: bool = True
) -> NormalizedJob:
    attempt = NormalizedAttempt(
        job_source_id="1",
        source_attempt_id="galaxy-0",
        runner=runner,
        outcome="ok",
        tool_started_at=datetime(2026, 9, 21, 12, tzinfo=UTC) if started else None,
        tool_finished_at=datetime(2026, 9, 21, 12, 1, tzinfo=UTC) if started else None,
    )
    return NormalizedJob(
        source_id="1",
        owner_source_id="1",
        tool_id="upload1",
        state="ok" if started else "paused",
        created_at=datetime(2026, 9, 21, 11, 59, tzinfo=UTC),
        updated_at=datetime(2026, 9, 21, 12, 1, tzinfo=UTC),
        runner=runner,
        destination=destination,
        resource_hints={"core.galaxy_slots": "1"},
        attempts=(attempt,),
    )


def test_baseline_destination_gets_existing_capacity() -> None:
    classified = classify_job(job(), PROFILE)
    lifetime = classified.attempts[0].lifetimes[0]
    assert lifetime.capacity_relationship == CapacityRelationship.existing
    assert lifetime.resource_uid == "galaxy-baseline-vm"
    assert lifetime.facts["policy_version"] == "anvil-baseline-v1"


def test_work_without_execution_evidence_gets_no_interval() -> None:
    classified = classify_job(job(started=False), PROFILE)
    assert classified.attempts[0].lifetimes == ()


def test_unmapped_destination_is_left_for_provider_evidence() -> None:
    classified = classify_job(job(runner="gcp_batch", destination="gcp_batch"), PROFILE)
    assert classified.attempts[0].lifetimes == ()


def test_absent_policy_leaves_capacity_unclassified() -> None:
    classified = classify_job(job(), None)
    assert classified.attempts[0].lifetimes == ()
