"""GCP Batch, Compute and Logging observation.

Galaxy stores only a short Batch job name, so every read combines it with the
configured project and location and keeps the full resolved resource name and
UID. Provider retries appear in *task* status events, not in the top-level job
status, and a retry can reuse the same VM: the same-VM retry captured for CI job
336 is the reference case. Each distinct VM becomes one chargeable lifetime that
several attempts may share.

Instance identities parsed out of task-event descriptions are labeled as
text-parsed correlation hints, because that text is not a stable structured
field. Live Compute reads and Logging operation boundaries are preferred when
available.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from rainstone.adapters.contracts import (
    NormalizedAttempt,
    NormalizedGap,
    NormalizedLifetime,
    NormalizedSegment,
    ObservationBatch,
)
from rainstone.models import CapacityRelationship

SOURCE_NAME = "gcp_batch"
JOB_LABEL = "galaxy-job-id"
METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
INSTANCE_REFERENCE = re.compile(r"zones/(?P<zone>[a-z0-9-]+)/instances/(?P<instance>\d+)")


class CloudAccessDenied(RuntimeError):
    """The deployment's identity lacks a permission this observation needs.

    Denied is not the same as absent: a missing resource is a normal answer,
    while a denial is a capability gap an operator must see.
    """

    def __init__(self, operation: str, detail: str = "") -> None:
        super().__init__(f"{operation} was denied{': ' + detail if detail else ''}")
        self.operation = operation
ATTEMPT_REFERENCE = re.compile(r"Attempt (?P<ordinal>\d+) failed")


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    # Provider timestamps carry nanosecond precision; Python parses microseconds.
    match = re.match(r"(.*\.\d{6})\d*(?P<offset>[+-]\d{2}:\d{2})$", text)
    if match:
        text = f"{match.group(1)}{match.group('offset')}"
    return datetime.fromisoformat(text).astimezone(UTC)


@dataclass(frozen=True)
class BatchTarget:
    """One Galaxy job whose Batch resource should be observed."""

    job_source_id: str
    external_id: str
    project: str
    location: str
    active: bool = False


@dataclass(frozen=True)
class InstanceObservation:
    instance_id: str
    zone: str
    project: str
    created_at: datetime | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    machine_type: str | None = None
    purchase_model: str | None = None
    labels: dict = field(default_factory=dict)
    source: str = "compute_instance"


class GcpClient(Protocol):
    def get_batch_job(self, project: str, location: str, job_id: str) -> dict | None: ...

    def list_batch_tasks(self, job_name: str, group: str = "group0") -> list[dict]: ...

    def get_instance(self, project: str, zone: str, instance_id: str) -> dict | None: ...

    def instance_lifecycle_entries(
        self, project: str, instance_id: str, *, after: datetime | None = None
    ) -> list[dict]: ...

    def probe_access(self, project: str, location: str) -> dict[str, str]: ...


class HttpGcpClient:
    """REST access using the deployment's own runtime identity.

    The access token comes from VM or workload metadata; a developer `gcloud`
    session is never used, and no token value is logged or persisted.
    """

    def __init__(self, *, token_provider: Callable[[], str] | None = None, page_limit: int = 100):
        self._token_provider = token_provider or self._metadata_token
        self._page_limit = page_limit

    @staticmethod
    def _metadata_token() -> str:
        request = urllib.request.Request(
            METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return json.load(response)["access_token"]

    def _get(self, url: str, params: dict | None = None, *, operation: str = "read") -> dict | None:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._token_provider()}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise CloudAccessDenied(operation, error.reason or "") from error
            if error.code == 404:
                return None
            raise

    def _post(self, url: str, payload: dict, *, operation: str = "read") -> dict | None:
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._token_provider()}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise CloudAccessDenied(operation, error.reason or "") from error
            if error.code == 404:
                return None
            raise

    def get_batch_job(self, project: str, location: str, job_id: str) -> dict | None:
        return self._get(
            "https://batch.googleapis.com/v1/"
            f"projects/{project}/locations/{location}/jobs/{job_id}",
            operation="batch.jobs.get",
        )

    def list_batch_tasks(self, job_name: str, group: str = "group0") -> list[dict]:
        tasks: list[dict] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"pageSize": self._page_limit}
            if page_token:
                params["pageToken"] = page_token
            payload = self._get(
                f"https://batch.googleapis.com/v1/{job_name}/taskGroups/{group}/tasks",
                params,
                operation="batch.tasks.list",
            )
            if not payload:
                return tasks
            tasks.extend(payload.get("tasks", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                return tasks

    def get_instance(self, project: str, zone: str, instance_id: str) -> dict | None:
        return self._get(
            "https://compute.googleapis.com/compute/v1/"
            f"projects/{project}/zones/{zone}/instances/{instance_id}",
            operation="compute.instances.get",
        )

    def instance_lifecycle_entries(
        self, project: str, instance_id: str, *, after: datetime | None = None
    ) -> list[dict]:
        window = (
            f' AND timestamp>="{after.isoformat()}"' if after else ""
        )
        query = (
            f'resource.type="gce_instance" AND resource.labels.instance_id="{instance_id}"'
            ' AND (protoPayload.methodName="v1.compute.instances.insert"'
            ' OR protoPayload.methodName="v1.compute.instances.delete")'
            f"{window}"
        )
        entries: list[dict] = []
        page_token: str | None = None
        while True:
            payload = self._post(
                "https://logging.googleapis.com/v2/entries:list",
                operation="logging.logEntries.list",
                payload={
                    "resourceNames": [f"projects/{project}"],
                    "filter": query,
                    "orderBy": "timestamp asc",
                    "pageSize": self._page_limit,
                    **({"pageToken": page_token} if page_token else {}),
                },
            )
            if not payload:
                return entries
            entries.extend(payload.get("entries", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                return entries


    def probe_access(self, project: str, location: str) -> dict[str, str]:
        """Exercise the operations each enabled observation actually needs.

        A probe reports `ok`, `denied` or `unavailable`; a missing resource is
        `ok`, because absence is a valid answer from an API that answered.
        """
        # Every operation collection performs is probed, not only the listings:
        # list permission does not imply get, and a missing resource answers
        # 404, which is a permitted call rather than a denial.
        missing = "rainstone-capability-probe"
        probes = {
            "batch.jobs.list": (
                self._get,
                (
                    "https://batch.googleapis.com/v1/"
                    f"projects/{project}/locations/{location}/jobs",
                    {"pageSize": 1},
                ),
            ),
            "batch.jobs.get": (
                self._get,
                (
                    "https://batch.googleapis.com/v1/"
                    f"projects/{project}/locations/{location}/jobs/{missing}",
                    None,
                ),
            ),
            "batch.tasks.list": (
                self._get,
                (
                    "https://batch.googleapis.com/v1/"
                    f"projects/{project}/locations/{location}/jobs/{missing}"
                    "/taskGroups/group0/tasks",
                    {"pageSize": 1},
                ),
            ),
            "compute.instances.get": (
                self._get,
                (
                    "https://compute.googleapis.com/compute/v1/"
                    f"projects/{project}/zones/{location}-a/instances/{missing}",
                    None,
                ),
            ),
            "compute.instances.list": (
                self._get,
                (
                    "https://compute.googleapis.com/compute/v1/"
                    f"projects/{project}/aggregated/instances",
                    {"maxResults": 1},
                ),
            ),
            "logging.logEntries.list": (
                self._post,
                (
                    "https://logging.googleapis.com/v2/entries:list",
                    {
                        "resourceNames": [f"projects/{project}"],
                        "filter": 'resource.type="gce_instance"',
                        "pageSize": 1,
                    },
                ),
            ),
        }
        results: dict[str, str] = {}
        for operation, (call, arguments) in probes.items():
            try:
                call(*arguments, operation=operation)
                results[operation] = "ok"
            except CloudAccessDenied:
                results[operation] = "denied"
            except OSError as error:
                results[operation] = f"unavailable: {type(error).__name__}"
        return results


@dataclass
class TaskAttempt:
    task_index: int
    attempt_ordinal: int
    outcome: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    assigned_at: datetime | None = None
    exit_code: int | None = None
    instance_id: str | None = None
    zone: str | None = None
    identity_source: str = "unknown"


def _task_index(task: dict) -> int:
    name = task.get("name", "")
    tail = name.rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def parse_task_attempts(task: dict) -> list[TaskAttempt]:
    """Split one task's status events into per-attempt execution records.

    A `RETRIED` event closes the current attempt and names its ordinal, so a
    successful final state never hides an earlier failed attempt.
    """
    index = _task_index(task)
    events = task.get("status", {}).get("statusEvents", []) or []
    attempts: list[TaskAttempt] = []
    current = TaskAttempt(task_index=index, attempt_ordinal=0, outcome="unknown")
    ordinal = 0
    for event in events:
        description = event.get("description") or ""
        occurred = _time(event.get("eventTime"))
        reference = INSTANCE_REFERENCE.search(description)
        if reference:
            current.instance_id = reference.group("instance")
            current.zone = reference.group("zone")
            current.identity_source = "task_event_text"
        state = event.get("taskState")
        kind = event.get("type")
        if state == "ASSIGNED":
            current.assigned_at = current.assigned_at or occurred
        elif state == "RUNNING":
            current.started_at = current.started_at or occurred
        if kind == "FAILED":
            current.finished_at = occurred
            current.outcome = "failed"
            current.exit_code = (event.get("taskExecution") or {}).get("exitCode")
        elif kind == "SUCCEEDED":
            current.finished_at = occurred
            current.outcome = "succeeded"
        elif kind == "RETRIED":
            match = ATTEMPT_REFERENCE.search(description)
            current.attempt_ordinal = int(match.group("ordinal")) if match else ordinal
            current.finished_at = current.finished_at or occurred
            if current.outcome == "unknown":
                current.outcome = "failed"
            attempts.append(current)
            ordinal = current.attempt_ordinal + 1
            current = TaskAttempt(
                task_index=index,
                attempt_ordinal=ordinal,
                outcome="unknown",
                instance_id=current.instance_id,
                zone=current.zone,
                identity_source=current.identity_source,
            )
    if current.outcome == "unknown":
        current.outcome = (task.get("status", {}).get("state") or "unknown").lower()
    current.attempt_ordinal = ordinal
    attempts.append(current)
    return attempts


def _instance_from_compute(payload: dict, project: str) -> InstanceObservation:
    scheduling = payload.get("scheduling", {})
    zone = (payload.get("zone") or "").rsplit("/", 1)[-1]
    return InstanceObservation(
        instance_id=str(payload["id"]),
        zone=zone,
        project=project,
        created_at=_time(payload.get("creationTimestamp")),
        started_at=_time(payload.get("lastStartTimestamp")),
        stopped_at=_time(payload.get("lastStopTimestamp")),
        machine_type=(payload.get("machineType") or "").rsplit("/", 1)[-1] or None,
        purchase_model=(scheduling.get("provisioningModel") or "").lower() or None,
        labels=payload.get("labels", {}) or {},
        source="compute_instance",
    )


def _lifecycle_bounds(entries: Sequence[dict]) -> dict:
    """Recover insert/delete operation boundaries after a VM is deleted."""
    insert_last = delete_first = None
    operations: list[str] = []
    for entry in entries:
        method = (entry.get("protoPayload") or {}).get("methodName") or entry.get("method")
        operation = entry.get("operation") or {}
        timestamp = _time(entry.get("timestamp"))
        if operation.get("id") and operation["id"] not in operations:
            operations.append(operation["id"])
        if method and method.endswith("instances.insert") and operation.get("last"):
            insert_last = timestamp
        if method and method.endswith("instances.delete") and operation.get("first"):
            delete_first = timestamp
    return {
        "insert_completed": insert_last,
        "delete_requested": delete_first,
        "operation_ids": operations,
    }


def _machine_shape(job: dict) -> tuple[str | None, str | None]:
    groups = (job.get("status", {}).get("taskGroups") or {}).values()
    for group in groups:
        for instance in group.get("instances", []) or []:
            machine = instance.get("machineType")
            model = (instance.get("provisioningModel") or "").lower() or None
            if machine:
                return machine, model
    for policy in job.get("instances_policy") or job.get("allocationPolicy", {}).get(
        "instances", []
    ):
        shape = policy.get("policy", {})
        if shape.get("machineType"):
            return shape["machineType"], (shape.get("provisioningModel") or "").lower() or None
    return None, None


def _requested_resources(job: dict) -> tuple[Decimal | None, Decimal | None]:
    groups = job.get("task_groups") or job.get("taskGroups") or []
    for group in groups:
        resource = group.get("computeResource") or group.get("taskSpec", {}).get(
            "computeResource", {}
        )
        if resource:
            cpu = resource.get("cpuMilli")
            memory = resource.get("memoryMib")
            return (
                Decimal(str(cpu)) / 1000 if cpu is not None else None,
                Decimal(str(memory)) if memory is not None else None,
            )
    return None, None


def normalize_batch_job(
    job: dict,
    tasks: Sequence[dict],
    *,
    project: str,
    location: str,
    instances: dict[str, InstanceObservation] | None = None,
    lifecycles: dict[str, dict] | None = None,
    job_source_id: str | None = None,
) -> list[NormalizedAttempt]:
    """Build attempts and the lifetimes they share for one Batch resource."""
    labelled = (job.get("labels") or {}).get(JOB_LABEL)
    source_id = job_source_id or (str(labelled) if labelled else None)
    if not source_id:
        return []
    machine_type, purchase_model = _machine_shape(job)
    vcpu, memory = _requested_resources(job)
    instances = instances or {}
    lifecycles = lifecycles or {}
    attempts: list[NormalizedAttempt] = []
    for task in tasks:
        for parsed in parse_task_attempts(task):
            lifetime = _lifetime_for(
                parsed,
                job=job,
                project=project,
                location=location,
                machine_type=machine_type,
                purchase_model=purchase_model,
                vcpu=vcpu,
                memory=memory,
                instance=instances.get(parsed.instance_id or ""),
                lifecycle=lifecycles.get(parsed.instance_id or ""),
            )
            attempts.append(
                NormalizedAttempt(
                    job_source_id=source_id,
                    source_attempt_id=(
                        f"batch:{job['uid']}:task-{parsed.task_index}:attempt-{parsed.attempt_ordinal}"
                    ),
                    runner=SOURCE_NAME,
                    outcome=parsed.outcome,
                    external_id=job["name"],
                    provider_outcome=job.get("status", {}).get("state"),
                    exit_code=parsed.exit_code,
                    task_index=parsed.task_index,
                    attempt_ordinal=parsed.attempt_ordinal,
                    tool_started_at=parsed.started_at,
                    tool_finished_at=parsed.finished_at,
                    lifetimes=(lifetime,) if lifetime else (),
                    correlation="galaxy_job_label",
                    facts={
                        "batch_uid": job["uid"],
                        "batch_name": job["name"],
                        "assigned_at": parsed.assigned_at.isoformat() if parsed.assigned_at else None,
                        "vm_identity_source": parsed.identity_source,
                    },
                )
            )
    return attempts


def _lifetime_for(
    parsed: TaskAttempt,
    *,
    job: dict,
    project: str,
    location: str,
    machine_type: str | None,
    purchase_model: str | None,
    vcpu: Decimal | None,
    memory: Decimal | None,
    instance: InstanceObservation | None,
    lifecycle: dict | None,
) -> NormalizedLifetime | None:
    if not parsed.instance_id:
        return None
    zone = (instance.zone if instance else None) or parsed.zone
    start = end = None
    method = "batch_task_events"
    facts: dict[str, Any] = {
        "batch_uid": job["uid"],
        "batch_name": job["name"],
        "location": location,
        "vm_identity_source": parsed.identity_source,
        "verified_vm_identity": bool(instance),
    }
    if instance:
        start = instance.started_at or instance.created_at
        end = instance.stopped_at
        method = "compute_instance_timestamps"
        facts["provider_labels"] = instance.labels
        facts["compute_machine_type"] = instance.machine_type
    if lifecycle and (lifecycle.get("insert_completed") or lifecycle.get("delete_requested")):
        start = lifecycle.get("insert_completed") or start
        end = lifecycle.get("delete_requested") or end
        method = "compute_insert_complete_to_delete_request"
        facts["lifecycle_operation_ids"] = lifecycle.get("operation_ids", [])
        facts["lifecycle_uncertainty"] = (
            "Audit operation markers approximate the billable lifetime; they are not an invoice."
        )
    if start is None:
        start = parsed.assigned_at or parsed.started_at
        end = end or parsed.finished_at
        facts["missing_provisioning_overhead"] = True
    segments = (
        NormalizedSegment(
            source_segment_id="lifetime",
            observed_start=start,
            observed_end=end,
            timing_method=method,
            facts={},
        ),
    )
    return NormalizedLifetime(
        provider="gcp",
        # Project, zone and numeric instance ID identify a GCE VM; retries that
        # reuse the VM resolve to this same key and are charged once.
        resource_key=f"gce:{project}/{zone}/{parsed.instance_id}",
        resource_uid=parsed.instance_id,
        capacity_relationship=CapacityRelationship.dedicated,
        timing_method=method,
        project=project,
        zone=zone,
        region=location,
        machine_type=(instance.machine_type if instance else None) or machine_type,
        purchase_model=(instance.purchase_model if instance else None) or purchase_model or "on_demand",
        observed_start=start,
        observed_end=end,
        requested_vcpu=vcpu,
        requested_memory_mib=memory,
        segments=segments,
        facts=facts,
    )


class BatchCollector:
    """Observes a bounded slice of known Batch targets per cycle."""

    source_name = SOURCE_NAME

    def __init__(
        self,
        client: GcpClient,
        targets: Callable[[], Sequence[BatchTarget]],
        *,
        max_targets: int = 25,
        terminal_share: float = 0.2,
        enrich_compute: bool = True,
        enrich_logging: bool = True,
        now: datetime | None = None,
    ) -> None:
        self._client = client
        self._targets = targets
        self._max_targets = max_targets
        self._terminal_share = terminal_share
        self._enrich_compute = enrich_compute
        self._enrich_logging = enrich_logging
        self._now = now

    def _clock(self) -> datetime:
        return self._now or datetime.now(UTC)

    def _page(
        self, items: Sequence[BatchTarget], offset: int, slots: int
    ) -> tuple[list[BatchTarget], int, bool]:
        """One non-overlapping page, with the offset wrapping at the end."""
        if not items or slots <= 0:
            return [], 0, True
        start = offset if offset < len(items) else 0
        window = list(items[start : start + slots])
        next_offset = start + len(window)
        if next_offset >= len(items):
            return window, 0, True
        return window, next_offset, False

    def _select(
        self, targets: Sequence[BatchTarget], state: dict
    ) -> tuple[list[BatchTarget], bool]:
        """Page active targets fairly while reserving terminal reconciliation.

        Always taking the first N active targets starves the rest whenever the
        active set is larger than one page, and leaves reconciliation no slots
        while work keeps arriving. Both lists page with their own durable
        cursor; when the budget is too small to reserve a reconciliation slot,
        the next cycle gives terminal work the first slots instead, so neither
        list can be starved indefinitely.
        """
        active = [target for target in targets if target.active]
        terminal = [target for target in targets if not target.active]
        reserved = (
            min(len(terminal), max(1, int(self._max_targets * self._terminal_share)))
            if terminal
            else 0
        )
        terminal_first = bool(state.pop("terminal_due", False)) and terminal
        if terminal_first:
            terminal_slots = min(len(terminal), self._max_targets)
            active_slots = max(self._max_targets - terminal_slots, 0)
        elif active:
            active_slots = max(self._max_targets - reserved, 1)
            terminal_slots = 0
        else:
            active_slots, terminal_slots = 0, min(len(terminal), self._max_targets)

        active_page, active_offset, active_done = self._page(
            active, int(state.get("active_offset", 0)), active_slots
        )
        if not terminal_first:
            terminal_slots = max(self._max_targets - len(active_page), 0)
        terminal_page, terminal_offset, _ = self._page(
            terminal, int(state.get("terminal_offset", 0)), terminal_slots
        )
        state["active_offset"] = active_offset
        state["terminal_offset"] = terminal_offset
        if terminal and not terminal_page and active_done:
            # This cycle had no room for reconciliation; the next one leads with
            # it rather than letting a busy active set starve it.
            state["terminal_due"] = True
        return [*active_page, *terminal_page], active_done

    def collect(self, cursor: dict) -> ObservationBatch:
        started = self._clock()
        state = dict(cursor)
        targets = list(self._targets())
        selected, active_done = self._select(targets, state)
        attempts: list[NormalizedAttempt] = []
        gaps: list[NormalizedGap] = []
        unresolved = 0
        for target in selected:
            try:
                job = self._client.get_batch_job(
                    target.project, target.location, target.external_id
                )
            except CloudAccessDenied as denial:
                # A denied primary read is a capability gap, not a missing job.
                raise denial
            if job is None:
                unresolved += 1
                gaps.append(
                    NormalizedGap(
                        source=SOURCE_NAME,
                        kind="batch_resource_unavailable",
                        detail=(
                            "Batch job "
                            f"projects/{target.project}/locations/{target.location}/jobs/"
                            f"{target.external_id} was not readable for Galaxy job "
                            f"{target.job_source_id}."
                        ),
                        gap_end=started,
                        recoverable=True,
                    )
                )
                continue
            tasks = self._client.list_batch_tasks(job["name"])
            try:
                instances, lifecycles = self._enrich(job, tasks, target)
            except CloudAccessDenied as denial:
                # Enrichment is optional: degrade with a visible gap rather than
                # failing collection of the Batch record itself.
                instances, lifecycles = {}, {}
                gaps.append(
                    NormalizedGap(
                        source=SOURCE_NAME,
                        kind="cloud_enrichment_denied",
                        detail=(
                            f"{denial.operation} is denied, so VM lifetimes for Galaxy job "
                            f"{target.job_source_id} use task events only."
                        ),
                        gap_end=started,
                        recoverable=True,
                    )
                )
            attempts.extend(
                normalize_batch_job(
                    job,
                    tasks,
                    project=target.project,
                    location=target.location,
                    instances=instances,
                    lifecycles=lifecycles,
                    job_source_id=target.job_source_id,
                )
            )
        return ObservationBatch(
            source=SOURCE_NAME,
            observed_at=started,
            attempts=tuple(attempts),
            gaps=tuple(gaps),
            cursor=state,
            metrics={
                "targets": len(targets),
                "active_targets": sum(1 for target in targets if target.active),
                "observed": len(selected),
                "attempts": len(attempts),
                "unresolved": unresolved,
                "active_offset": state["active_offset"],
                "terminal_offset": state["terminal_offset"],
            },
            # Another page of active work is drained immediately rather than
            # waiting a full refresh interval.
            exhausted=active_done,
        )

    def _enrich(
        self, job: dict, tasks: Sequence[dict], target: BatchTarget
    ) -> tuple[dict[str, InstanceObservation], dict[str, dict]]:
        instances: dict[str, InstanceObservation] = {}
        lifecycles: dict[str, dict] = {}
        references = {
            (parsed.instance_id, parsed.zone)
            for task in tasks
            for parsed in parse_task_attempts(task)
            if parsed.instance_id
        }
        for instance_id, zone in references:
            if self._enrich_compute and zone:
                payload = self._client.get_instance(target.project, zone, instance_id)
                if payload:
                    instances[instance_id] = _instance_from_compute(payload, target.project)
            if instance_id not in instances and self._enrich_logging:
                entries = self._client.instance_lifecycle_entries(target.project, instance_id)
                if entries:
                    lifecycles[instance_id] = _lifecycle_bounds(entries)
        return instances, lifecycles
