import json
from decimal import Decimal
from pathlib import Path

from rainstone.adapters.gcp_batch import (
    BatchCollector,
    BatchTarget,
    _instance_from_compute,
    _lifecycle_bounds,
    normalize_batch_job,
    parse_task_attempts,
)

CONTRACT = Path("fixtures/contract")


def load(name: str) -> dict | list:
    return json.loads((CONTRACT / name).read_text())


class FakeGcpClient:
    """Returns saved contract payloads and counts pagination-sensitive calls."""

    def __init__(self, *, jobs: dict, instance: dict | None = None, entries: list | None = None):
        self._jobs = jobs
        self._instance = instance
        self._entries = entries or []
        self.task_calls = 0

    def get_batch_job(self, project: str, location: str, job_id: str) -> dict | None:
        return self._jobs.get(job_id)

    def list_batch_tasks(self, job_name: str, group: str = "group0") -> list[dict]:
        self.task_calls += 1
        for job in self._jobs.values():
            if job["name"] == job_name:
                return job["tasks"]
        return []

    def get_instance(self, project: str, zone: str, instance_id: str) -> dict | None:
        if self._instance and self._instance["id"] == instance_id:
            return self._instance
        return None

    def instance_lifecycle_entries(self, project: str, instance_id: str, *, after=None) -> list:
        return self._entries


def test_task_events_expose_a_retry_on_the_same_vm() -> None:
    job = load("batch-job-retry.json")
    attempts = parse_task_attempts(job["tasks"][0])
    assert [attempt.attempt_ordinal for attempt in attempts] == [0, 1]
    assert attempts[0].outcome == "failed"
    assert attempts[0].exit_code == 125
    assert attempts[1].outcome == "succeeded"
    assert {attempt.instance_id for attempt in attempts} == {"8649021727128195799"}
    assert {attempt.identity_source for attempt in attempts} == {"task_event_text"}


def test_same_vm_retry_produces_one_shared_lifetime() -> None:
    job = load("batch-job-retry.json")
    attempts = normalize_batch_job(
        job, job["tasks"], project="demo-project", location="us-east4"
    )
    assert len(attempts) == 2
    keys = {attempt.lifetimes[0].resource_key for attempt in attempts}
    assert keys == {"gce:demo-project/us-east4-c/8649021727128195799"}
    assert {attempt.job_source_id for attempt in attempts} == {"336"}
    assert attempts[0].lifetimes[0].machine_type == "n2-standard-8"
    assert attempts[0].lifetimes[0].requested_vcpu == Decimal("8")
    assert attempts[0].lifetimes[0].facts["missing_provisioning_overhead"] is True


def test_logging_enrichment_supplies_the_lifecycle_window() -> None:
    job = load("batch-job-retry.json")
    bounds = _lifecycle_bounds(load("logging-entries.json"))
    attempts = normalize_batch_job(
        job,
        job["tasks"],
        project="demo-project",
        location="us-east4",
        lifecycles={"8649021727128195799": bounds},
    )
    lifetime = attempts[0].lifetimes[0]
    seconds = (lifetime.observed_end - lifetime.observed_start).total_seconds()
    assert lifetime.timing_method == "compute_insert_complete_to_delete_request"
    assert round(seconds, 6) == 546.386123
    assert "lifecycle_uncertainty" in lifetime.facts


def test_live_compute_identity_is_preferred_and_marked_verified() -> None:
    job = load("batch-job-retry.json")
    instance = _instance_from_compute(load("compute-instance.json"), "demo-project")
    attempts = normalize_batch_job(
        job,
        job["tasks"],
        project="demo-project",
        location="us-east4",
        instances={instance.instance_id: instance},
    )
    lifetime = attempts[0].lifetimes[0]
    assert lifetime.timing_method == "compute_instance_timestamps"
    assert lifetime.facts["verified_vm_identity"] is True
    assert lifetime.facts["provider_labels"]["batch-job-uid"] == "galaxy-batch-demo-336-uid"


def test_provider_success_never_overwrites_a_galaxy_error() -> None:
    job = load("batch-job-galaxy-error.json")
    attempts = normalize_batch_job(
        job, job["tasks"], project="demo-project", location="us-east4"
    )
    assert attempts[0].provider_outcome == "SUCCEEDED"
    assert attempts[0].outcome == "succeeded"
    # The Galaxy state stays on the job record, which this adapter never writes.
    assert attempts[0].job_source_id == "53"


def test_collector_reports_unreadable_resources_as_gaps() -> None:
    client = FakeGcpClient(jobs={})
    collector = BatchCollector(
        client,
        lambda: [
            BatchTarget(
                job_source_id="336",
                external_id="galaxy-batch-demo-336",
                project="demo-project",
                location="us-east4",
                active=True,
            )
        ],
    )
    batch = collector.collect({})
    assert batch.attempts == ()
    assert batch.gaps[0].kind == "batch_resource_unavailable"
    assert batch.gaps[0].recoverable is True
    assert batch.metrics["unresolved"] == 1


def test_active_work_leads_and_terminal_work_still_gets_a_turn() -> None:
    retry = load("batch-job-retry.json")
    error = load("batch-job-galaxy-error.json")
    client = FakeGcpClient(jobs={"galaxy-batch-demo-336": retry, "galaxy-batch-demo-53": error})
    targets = [
        BatchTarget("336", "galaxy-batch-demo-336", "demo-project", "us-east4", active=True),
        BatchTarget("53", "galaxy-batch-demo-53", "demo-project", "us-east4"),
    ]
    collector = BatchCollector(client, lambda: targets, max_targets=1)
    first = collector.collect({})
    assert {attempt.job_source_id for attempt in first.attempts} == {"336"}
    # A single-slot budget cannot reserve reconciliation, so the next cycle
    # leads with it instead of starving it.
    second = collector.collect(first.cursor)
    assert {attempt.job_source_id for attempt in second.attempts} == {"53"}


def targets(active: int, terminal: int = 0) -> list[BatchTarget]:
    return [
        BatchTarget(f"a{index}", f"job-a{index}", "demo-project", "us-east4", active=True)
        for index in range(active)
    ] + [
        BatchTarget(f"t{index}", f"job-t{index}", "demo-project", "us-east4")
        for index in range(terminal)
    ]


def test_active_targets_beyond_one_page_are_not_starved() -> None:
    """Thirty running jobs must all be observed, not the same first page."""
    collector = BatchCollector(FakeGcpClient(jobs={}), lambda: targets(30), max_targets=25)
    cursor: dict = {}
    seen: list[str] = []
    for _ in range(2):
        batch = collector.collect(cursor)
        cursor = dict(batch.cursor)
        seen.extend(gap.detail for gap in batch.gaps)
    selected = {detail.split("Galaxy job ")[-1].rstrip(".") for detail in seen}
    assert selected == {f"a{index}" for index in range(30)}


def test_a_page_of_active_work_is_drained_before_the_next_interval() -> None:
    collector = BatchCollector(FakeGcpClient(jobs={}), lambda: targets(30), max_targets=25)
    first = collector.collect({})
    assert first.exhausted is False
    second = collector.collect(first.cursor)
    assert second.exhausted is True


def test_terminal_reconciliation_keeps_capacity_under_sustained_load() -> None:
    collector = BatchCollector(
        FakeGcpClient(jobs={}), lambda: targets(100, terminal=10), max_targets=25
    )
    batch = collector.collect({})
    observed = {detail.split("Galaxy job ")[-1].rstrip(".") for detail in
                (gap.detail for gap in batch.gaps)}
    assert any(name.startswith("t") for name in observed)
    assert sum(1 for name in observed if name.startswith("a")) >= 20


def test_target_cursors_survive_restart_and_shrinking_lists() -> None:
    collector = BatchCollector(FakeGcpClient(jobs={}), lambda: targets(30), max_targets=25)
    first = collector.collect({})
    # A restarted collector resumes from the persisted cursor.
    resumed = BatchCollector(FakeGcpClient(jobs={}), lambda: targets(30), max_targets=25)
    second = resumed.collect(first.cursor)
    assert second.cursor["active_offset"] == 0
    # Targets disappearing between cycles must not skip the remaining ones.
    shrunk = BatchCollector(FakeGcpClient(jobs={}), lambda: targets(3), max_targets=25)
    batch = shrunk.collect({"active_offset": 27, "terminal_offset": 0})
    observed = {gap.detail.split("Galaxy job ")[-1].rstrip(".") for gap in batch.gaps}
    assert observed == {"a0", "a1", "a2"}


def test_probes_cover_every_operation_collection_performs() -> None:
    """List permission does not imply get, so each call is probed."""
    from rainstone.adapters.gcp_batch import CloudAccessDenied, HttpGcpClient

    denied = {"compute.instances.get"}
    calls: list[str] = []
    client = HttpGcpClient(token_provider=lambda: "token")

    def fake_get(url, params=None, *, operation="read"):
        calls.append(operation)
        if operation in denied:
            raise CloudAccessDenied(operation, "Forbidden")
        return None

    def fake_post(url, payload=None, *, operation="read"):
        calls.append(operation)
        return {}

    client._get = fake_get  # noqa: SLF001 - exercising the probe matrix
    client._post = fake_post  # noqa: SLF001
    results = client.probe_access("demo-project", "us-east4")
    assert set(calls) == {
        "batch.jobs.list",
        "batch.jobs.get",
        "batch.tasks.list",
        "compute.instances.list",
        "compute.instances.get",
        "logging.logEntries.list",
    }
    # A missing resource is a permitted call; a denial is a capability gap.
    assert results["batch.jobs.get"] == "ok"
    assert results["compute.instances.get"] == "denied"
