"""The only writer of reporting facts.

Every adapter produces normalized records; this module upserts them with stable
source identities so repeated ingestion cannot duplicate cost. A batch is
committed before its cursor advances, which makes restart and replay safe.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from rainstone.adapters.contracts import (
    NormalizedAttempt,
    NormalizedGap,
    NormalizedInvocation,
    NormalizedJob,
    NormalizedLifetime,
    NormalizedMembership,
    NormalizedMetric,
    NormalizedOwner,
    NormalizedSegment,
    NormalizedStateEvent,
    ObservationBatch,
)
from rainstone.costing import calculate_tenant
from rainstone.models import (
    CapacityRelationship,
    DeploymentPolicy,
    ExecutionAttempt,
    InfrastructureInterval,
    IngestionEvent,
    IngestionState,
    Invocation,
    InvocationJob,
    Job,
    JobMetric,
    JobStateEvent,
    LifetimeAttempt,
    ObservationGap,
    Owner,
    PriceVersion,
    Quality,
    ResourceLifetime,
    ResourceSegment,
    Tenant,
)

NAMESPACE = uuid.UUID("22fcfbaa-52d7-4bce-af8f-452d26f79cc8")

# Higher-precedence evidence wins when several observations describe one
# resource lifetime; equal precedence keeps the newest observation.
TIMING_PRECEDENCE = {
    "provider_billable": 50,
    "compute_instance_timestamps": 40,
    "compute_insert_complete_to_delete_request": 35,
    "kubernetes_pod_occupancy": 25,
    "batch_task_events": 15,
    "configured_baseline_occupancy": 10,
    "galaxy_metrics": 5,
}


def stable_id(*parts: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, ":".join(parts))


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _set(obj: object, data: dict, fields: tuple[str, ...]) -> None:
    for field_name in fields:
        if field_name in data:
            setattr(obj, field_name, data[field_name])


def _precedence(method: str | None) -> int:
    return TIMING_PRECEDENCE.get(method or "", 0)


def upsert_owner(session: Session, tenant_id: uuid.UUID, owner: NormalizedOwner) -> uuid.UUID:
    owner_id = stable_id(str(tenant_id), "owner", owner.source_id)
    row = session.get(Owner, owner_id)
    if row is None:
        row = Owner(id=owner_id, tenant_id=tenant_id, source_id=owner.source_id, label=owner.label)
        session.add(row)
        session.flush()
    row.label = owner.label
    row.is_admin = owner.is_admin or bool(row.is_admin)
    return owner_id


def _ensure_owner_id(session: Session, tenant_id: uuid.UUID, source_id: str) -> uuid.UUID:
    """Keep a job attributable even before its owner record is collected."""
    owner_id = stable_id(str(tenant_id), "owner", source_id)
    if session.get(Owner, owner_id) is None:
        session.add(
            Owner(
                id=owner_id,
                tenant_id=tenant_id,
                source_id=source_id,
                label=f"Galaxy account {source_id}",
                is_admin=False,
            )
        )
        session.flush()
    return owner_id


def upsert_metric(session: Session, job_id: uuid.UUID, metric: NormalizedMetric) -> None:
    metric_id = stable_id(str(job_id), "metric", metric.plugin, metric.name)
    row = session.get(JobMetric, metric_id)
    if row is None:
        row = JobMetric(
            id=metric_id, job_id=job_id, plugin=metric.plugin, name=metric.name,
            numeric_value=metric.numeric_value,
        )
        session.add(row)
    else:
        row.numeric_value = metric.numeric_value


def upsert_state_event(session: Session, job_id: uuid.UUID, event: NormalizedStateEvent) -> None:
    event_id = stable_id(str(job_id), "state", event.state, event.occurred_at.isoformat())
    if session.get(JobStateEvent, event_id) is None:
        session.add(
            JobStateEvent(
                id=event_id, job_id=job_id, state=event.state, occurred_at=event.occurred_at
            )
        )


def upsert_lifetime(
    session: Session, tenant_id: uuid.UUID, lifetime: NormalizedLifetime
) -> uuid.UUID:
    lifetime_id = stable_id(str(tenant_id), "lifetime", lifetime.provider, lifetime.resource_key)
    # Look the resource up by its natural key, so rows carried over by a schema
    # migration are updated rather than duplicated.
    row = session.scalar(
        select(ResourceLifetime).where(
            ResourceLifetime.tenant_id == tenant_id,
            ResourceLifetime.provider == lifetime.provider,
            ResourceLifetime.resource_key == lifetime.resource_key,
        )
    )
    if row is None:
        row = ResourceLifetime(
            id=lifetime_id,
            tenant_id=tenant_id,
            provider=lifetime.provider,
            resource_key=lifetime.resource_key,
            resource_uid=lifetime.resource_uid,
            capacity_relationship=lifetime.capacity_relationship,
            observed_start=lifetime.observed_start,
            observed_end=lifetime.observed_end,
            timing_method=lifetime.timing_method,
            facts={},
        )
        session.add(row)
        session.flush()
    if _precedence(lifetime.timing_method) >= _precedence(row.timing_method):
        row.observed_start = lifetime.observed_start
        row.observed_end = lifetime.observed_end
        row.timing_method = lifetime.timing_method
    elif row.observed_end is None and lifetime.observed_end is not None:
        # Better evidence that cannot close a window must not hold it open for
        # good. A live Compute read is the best source for when a VM started,
        # and reports nothing at all once that VM is deleted — so the audit
        # trail's delete marker is the only evidence the window ever ends.
        # Filling a gap is not overwriting: the start keeps its better source,
        # and the end records the weaker one it came from.
        row.observed_end = lifetime.observed_end
        row.facts = {
            **(row.facts or {}),
            "observed_end_timing_method": lifetime.timing_method,
        }
    row.resource_uid = lifetime.resource_uid
    row.capacity_relationship = lifetime.capacity_relationship
    row.project = lifetime.project or row.project
    row.zone = lifetime.zone or row.zone
    row.region = lifetime.region or row.region
    row.machine_type = lifetime.machine_type or row.machine_type
    row.purchase_model = lifetime.purchase_model or row.purchase_model
    if lifetime.requested_vcpu is not None:
        row.requested_vcpu = lifetime.requested_vcpu
    if lifetime.requested_memory_mib is not None:
        row.requested_memory_mib = lifetime.requested_memory_mib
    row.facts = {**(row.facts or {}), **lifetime.facts}
    lifetime_id = row.id
    for segment in lifetime.segments:
        segment_id = stable_id(str(lifetime_id), "segment", segment.source_segment_id)
        existing = session.scalar(
            select(ResourceSegment).where(
                ResourceSegment.lifetime_id == lifetime_id,
                ResourceSegment.source_segment_id == segment.source_segment_id,
            )
        )
        if existing is None:
            session.add(
                ResourceSegment(
                    id=segment_id,
                    lifetime_id=lifetime_id,
                    source_segment_id=segment.source_segment_id,
                    observed_start=segment.observed_start,
                    observed_end=segment.observed_end,
                    timing_method=segment.timing_method,
                    facts=segment.facts,
                )
            )
        elif _precedence(segment.timing_method) >= _precedence(existing.timing_method):
            existing.observed_start = segment.observed_start
            existing.observed_end = segment.observed_end
            existing.timing_method = segment.timing_method
            existing.facts = {**(existing.facts or {}), **segment.facts}
    return lifetime_id


def upsert_attempt(
    session: Session, tenant_id: uuid.UUID, job_id: uuid.UUID, attempt: NormalizedAttempt
) -> uuid.UUID:
    attempt_id = stable_id(str(job_id), "attempt", attempt.source_attempt_id)
    row = session.get(ExecutionAttempt, attempt_id)
    if row is None:
        row = ExecutionAttempt(
            id=attempt_id,
            job_id=job_id,
            source_attempt_id=attempt.source_attempt_id,
            runner=attempt.runner,
            outcome=attempt.outcome,
            facts={},
        )
        session.add(row)
        session.flush()
    row.runner = attempt.runner
    row.outcome = attempt.outcome
    row.external_id = attempt.external_id or row.external_id
    # Galaxy and provider outcomes stay separate: a successful provider task
    # never overwrites a Galaxy error.
    row.provider_outcome = attempt.provider_outcome or row.provider_outcome
    if attempt.exit_code is not None:
        row.exit_code = attempt.exit_code
    if attempt.task_index is not None:
        row.task_index = attempt.task_index
    if attempt.attempt_ordinal is not None:
        row.attempt_ordinal = attempt.attempt_ordinal
    if attempt.tool_started_at is not None:
        row.tool_started_at = attempt.tool_started_at
    if attempt.tool_finished_at is not None:
        row.tool_finished_at = attempt.tool_finished_at
    row.facts = {**(row.facts or {}), **attempt.facts}
    for lifetime in attempt.lifetimes:
        lifetime_id = upsert_lifetime(session, tenant_id, lifetime)
        link = session.get(LifetimeAttempt, (lifetime_id, attempt_id))
        if link is None:
            session.add(
                LifetimeAttempt(
                    lifetime_id=lifetime_id,
                    attempt_id=attempt_id,
                    task_index=attempt.task_index,
                    attempt_ordinal=attempt.attempt_ordinal,
                    correlation=attempt.correlation,
                    facts={},
                )
            )
    return attempt_id


def upsert_job(session: Session, tenant_id: uuid.UUID, job: NormalizedJob) -> uuid.UUID:
    job_id = stable_id(str(tenant_id), "job", job.source_id)
    owner_id = _ensure_owner_id(session, tenant_id, job.owner_source_id)
    row = session.get(Job, job_id)
    if row is None:
        row = Job(
            id=job_id,
            tenant_id=tenant_id,
            source_id=job.source_id,
            owner_id=owner_id,
            tool_id=job.tool_id,
            state=job.state,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )
        session.add(row)
        session.flush()
    row.owner_id = owner_id
    row.tool_id = job.tool_id
    row.tool_version = job.tool_version
    row.state = job.state
    row.exit_code = job.exit_code
    row.runner = job.runner
    row.destination = job.destination
    row.handler = job.handler
    row.created_at = job.created_at
    row.updated_at = job.updated_at
    row.copied_from_source_id = job.copied_from_source_id
    row.resource_hints = {**(row.resource_hints or {}), **job.resource_hints}
    for metric in job.metrics:
        upsert_metric(session, job_id, metric)
    for event in job.state_events:
        upsert_state_event(session, job_id, event)
    for attempt in job.attempts:
        upsert_attempt(session, tenant_id, job_id, attempt)
    return job_id


def upsert_invocation(
    session: Session, tenant_id: uuid.UUID, invocation: NormalizedInvocation
) -> uuid.UUID:
    invocation_id = stable_id(str(tenant_id), "invocation", invocation.source_id)
    owner_id = _ensure_owner_id(session, tenant_id, invocation.owner_source_id)
    row = session.get(Invocation, invocation_id)
    if row is None:
        row = Invocation(
            id=invocation_id,
            tenant_id=tenant_id,
            source_id=invocation.source_id,
            owner_id=owner_id,
            workflow_name=invocation.workflow_name,
            state=invocation.state,
            created_at=invocation.created_at,
        )
        session.add(row)
        session.flush()
    row.owner_id = owner_id
    row.workflow_id = invocation.workflow_id
    row.workflow_family_id = invocation.workflow_family_id
    row.workflow_name = invocation.workflow_name
    row.workflow_version = invocation.workflow_version
    row.state = invocation.state
    row.membership_settled = invocation.membership_settled
    row.created_at = invocation.created_at
    row.parent_id = (
        stable_id(str(tenant_id), "invocation", invocation.parent_source_id)
        if invocation.parent_source_id
        else None
    )
    for membership in invocation.memberships:
        job_id = stable_id(str(tenant_id), "job", membership.job_source_id)
        if session.get(Job, job_id) is None:
            # The job page has not arrived yet; the next reconciliation pass
            # revisits this invocation.
            continue
        key = (invocation_id, job_id, membership.step_key)
        if session.get(InvocationJob, key) is None:
            session.add(
                InvocationJob(
                    invocation_id=invocation_id,
                    job_id=job_id,
                    step_key=membership.step_key,
                    relationship=membership.relationship,
                )
            )
    return invocation_id


def record_gap(session: Session, tenant_id: uuid.UUID, gap: NormalizedGap, observed_at: datetime) -> None:
    session.add(
        ObservationGap(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            source=gap.source,
            kind=gap.kind,
            detected_at=observed_at,
            gap_start=gap.gap_start,
            gap_end=gap.gap_end,
            recoverable=gap.recoverable,
            detail=gap.detail,
        )
    )


def batch_digest(batch: ObservationBatch) -> str:
    payload = json.dumps(
        {
            "source": batch.source,
            "jobs": [job.source_id for job in batch.jobs],
            "attempts": [
                (attempt.job_source_id, attempt.source_attempt_id)
                for attempt in [
                    *batch.attempts,
                    *[a for job in batch.jobs for a in job.attempts],
                ]
            ],
            "invocations": [invocation.source_id for invocation in batch.invocations],
            "observed_at": batch.observed_at.isoformat(),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def apply_batch(
    session: Session,
    tenant_id: uuid.UUID,
    batch: ObservationBatch,
    *,
    advance_cursor: bool = True,
) -> dict:
    """Persist one normalized batch and record its ingestion state."""
    for owner in batch.owners:
        upsert_owner(session, tenant_id, owner)
    session.flush()
    for job in batch.jobs:
        upsert_job(session, tenant_id, job)
    session.flush()
    orphans = 0
    for attempt in batch.attempts:
        job_id = stable_id(str(tenant_id), "job", attempt.job_source_id)
        if session.get(Job, job_id) is None:
            # Provider evidence can precede the Galaxy record; the next cycle
            # retries once the job exists.
            orphans += 1
            continue
        upsert_attempt(session, tenant_id, job_id, attempt)
    session.flush()
    for invocation in batch.invocations:
        upsert_invocation(session, tenant_id, invocation)
    for gap in batch.gaps:
        record_gap(session, tenant_id, gap, batch.observed_at)
    digest = batch_digest(batch)
    event_key = f"{batch.source}:{digest}"
    prior = session.scalar(
        select(IngestionEvent).where(
            IngestionEvent.tenant_id == tenant_id,
            IngestionEvent.source == batch.source,
            IngestionEvent.idempotency_key == event_key,
        )
    )
    now = datetime.now(UTC)
    if prior is None:
        session.add(
            IngestionEvent(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                source=batch.source,
                idempotency_key=event_key,
                payload_digest=digest,
                observed_at=batch.observed_at,
                ingested_at=now,
            )
        )
    else:
        prior.ingested_at = now
    state = _state(session, tenant_id, batch.source)
    state.last_attempt_at = now
    state.last_success_at = now
    state.consecutive_failures = 0
    state.error = None
    state.status = "degraded" if batch.gaps else "healthy"
    if advance_cursor:
        state.cursor = batch.cursor
    state.metrics = {
        **batch.metrics,
        "contract_version": batch.contract_version,
        "observed_at": batch.observed_at.isoformat(),
        "orphan_attempts": orphans,
        "gaps": len(batch.gaps),
    }
    session.flush()
    return {
        "source": batch.source,
        "jobs": len(batch.jobs),
        "attempts": len(batch.attempts),
        "invocations": len(batch.invocations),
        "gaps": len(batch.gaps),
        "orphan_attempts": orphans,
        "replayed": prior is not None,
        "exhausted": batch.exhausted,
    }


def _state(session: Session, tenant_id: uuid.UUID, source: str) -> IngestionState:
    state_id = stable_id(str(tenant_id), "state", source)
    state = session.get(IngestionState, state_id)
    if state is None:
        state = IngestionState(
            id=state_id, tenant_id=tenant_id, source=source, cursor={}, status="unknown",
            consecutive_failures=0, metrics={},
        )
        session.add(state)
    return state


def read_cursor(session: Session, tenant_id: uuid.UUID, source: str) -> dict:
    return dict(_state(session, tenant_id, source).cursor or {})


def record_failure(
    session: Session, tenant_id: uuid.UUID, source: str, error: str
) -> IngestionState:
    state = _state(session, tenant_id, source)
    state.last_attempt_at = datetime.now(UTC)
    state.consecutive_failures = (state.consecutive_failures or 0) + 1
    state.status = "failed"
    state.error = error[:2000]
    session.flush()
    return state


def _fixture_lifetimes(data: list[dict]) -> tuple[NormalizedLifetime, ...]:
    lifetimes: list[NormalizedLifetime] = []
    for item in data:
        segments = item.get("segments") or [
            {
                "source_segment_id": "lifetime",
                "observed_start": item.get("observed_start"),
                "observed_end": item.get("observed_end"),
                "timing_method": item["timing_method"],
            }
        ]
        lifetimes.append(
            NormalizedLifetime(
                provider=item["provider"],
                resource_key=item["resource_key"],
                resource_uid=item["resource_uid"],
                capacity_relationship=CapacityRelationship(item["capacity_relationship"]),
                timing_method=item["timing_method"],
                project=item.get("project"),
                zone=item.get("zone"),
                region=item.get("region"),
                machine_type=item.get("machine_type"),
                purchase_model=item.get("purchase_model"),
                observed_start=parse_time(item.get("observed_start")),
                observed_end=parse_time(item.get("observed_end")),
                requested_vcpu=(
                    Decimal(str(item["requested_vcpu"])) if item.get("requested_vcpu") is not None else None
                ),
                requested_memory_mib=(
                    Decimal(str(item["requested_memory_mib"]))
                    if item.get("requested_memory_mib") is not None
                    else None
                ),
                segments=tuple(
                    NormalizedSegment(
                        source_segment_id=segment["source_segment_id"],
                        observed_start=parse_time(segment.get("observed_start")),
                        observed_end=parse_time(segment.get("observed_end")),
                        timing_method=segment.get("timing_method", item["timing_method"]),
                        facts=segment.get("facts", {}),
                    )
                    for segment in segments
                ),
                facts=item.get("facts", {}),
            )
        )
    return tuple(lifetimes)


def _fixture_batch(data: dict) -> ObservationBatch:
    jobs: list[NormalizedJob] = []
    for item in data["jobs"]:
        attempts = tuple(
            NormalizedAttempt(
                job_source_id=item["source_id"],
                source_attempt_id=attempt["source_attempt_id"],
                runner=attempt["runner"],
                outcome=attempt["outcome"],
                external_id=attempt.get("external_id"),
                provider_outcome=attempt.get("provider_outcome"),
                exit_code=attempt.get("exit_code"),
                task_index=attempt.get("task_index"),
                attempt_ordinal=attempt.get("attempt_ordinal"),
                tool_started_at=parse_time(attempt.get("tool_started_at")),
                tool_finished_at=parse_time(attempt.get("tool_finished_at")),
                lifetimes=_fixture_lifetimes(attempt.get("lifetimes", [])),
                correlation=attempt.get("correlation", "fixture"),
                facts=attempt.get("facts", {}),
            )
            for attempt in item["attempts"]
        )
        jobs.append(
            NormalizedJob(
                source_id=item["source_id"],
                owner_source_id=item["owner_source_id"],
                tool_id=item["tool_id"],
                tool_version=item.get("tool_version"),
                state=item["state"],
                exit_code=item.get("exit_code"),
                runner=item.get("runner"),
                destination=item.get("destination"),
                handler=item.get("handler"),
                created_at=parse_time(item["created_at"]),
                updated_at=parse_time(item["updated_at"]),
                copied_from_source_id=item.get("copied_from_source_id"),
                resource_hints=item.get("resource_hints", {}),
                attempts=attempts,
            )
        )
    invocations = tuple(
        NormalizedInvocation(
            source_id=item["source_id"],
            owner_source_id=item["owner_source_id"],
            workflow_name=item["workflow_name"],
            state=item["state"],
            created_at=parse_time(item["created_at"]),
            workflow_id=item.get("workflow_id"),
            workflow_family_id=item.get("workflow_family_id"),
            workflow_version=item.get("workflow_version"),
            parent_source_id=item.get("parent_source_id"),
            membership_settled=item.get("membership_settled", True),
            memberships=tuple(
                NormalizedMembership(
                    job_source_id=membership["job_source_id"],
                    step_key=membership["step_key"],
                    relationship=membership["relationship"],
                )
                for membership in item["jobs"]
            ),
        )
        for item in data["invocations"]
    )
    return ObservationBatch(
        source="fixture",
        observed_at=parse_time(data["observed_at"]),
        owners=tuple(
            NormalizedOwner(
                source_id=owner["source_id"], label=owner["label"], is_admin=owner.get("is_admin", False)
            )
            for owner in data["owners"]
        ),
        jobs=tuple(jobs),
        invocations=invocations,
        cursor={"fixture_id": data["fixture_id"]},
        metrics={"fixture_id": data["fixture_id"]},
    )


def ingest_fixture(session: Session, path: Path) -> dict[str, int | str]:
    """Idempotently load a normalized fixture through the production path."""
    data = json.loads(path.read_text())
    tenant_data = data["tenant"]
    tenant_id = stable_id("tenant", tenant_data["slug"])
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        tenant = Tenant(id=tenant_id, slug=tenant_data["slug"], display_name=tenant_data["display_name"])
        session.add(tenant)
    _set(tenant, tenant_data, ("display_name", "source_version", "base_url", "capabilities"))
    tenant.synced_at = parse_time(data["observed_at"])

    for item in data["policies"]:
        item_id = stable_id(str(tenant_id), "policy", item["version"])
        obj = session.get(DeploymentPolicy, item_id)
        if obj is None:
            obj = DeploymentPolicy(id=item_id, tenant_id=tenant_id, version=item["version"])
            session.add(obj)
        obj.effective_from = parse_time(item["effective_from"])
        obj.effective_to = parse_time(item.get("effective_to"))
        _set(obj, item, ("baseline_resource_ids", "assumptions", "evidence"))

    for item in data["prices"]:
        item_id = stable_id(
            "price", item["catalog_id"], item["machine_type"], item["region"], item["purchase_model"]
        )
        obj = session.get(PriceVersion, item_id)
        if obj is None:
            obj = PriceVersion(id=item_id)
            session.add(obj)
        _set(
            obj,
            item,
            ("catalog_id", "machine_type", "provider", "region", "purchase_model", "currency", "provenance"),
        )
        obj.hourly_rate = Decimal(item["hourly_rate"])
        obj.observed_at = parse_time(item["observed_at"])
        obj.effective_from = parse_time(item.get("effective_from"))

    result = apply_batch(session, tenant_id, _fixture_batch(data))

    for item in data["infrastructure_intervals"]:
        item_id = stable_id(str(tenant_id), "infrastructure", item["source_id"])
        obj = session.get(InfrastructureInterval, item_id)
        if obj is None:
            obj = InfrastructureInterval(id=item_id, tenant_id=tenant_id, source_id=item["source_id"])
            session.add(obj)
        _set(obj, item, ("resource_uid", "machine_type", "region", "currency"))
        obj.observed_start = parse_time(item["observed_start"])
        obj.observed_end = parse_time(item["observed_end"])
        obj.amount = Decimal(item["amount"])
        obj.quality = Quality(item["quality"])

    session.flush()
    revision = calculate_tenant(session, tenant_id, reason="fixture ingestion")
    session.commit()
    return {
        "tenant": tenant_data["slug"],
        "jobs": len(data["jobs"]),
        "revision": str(revision.id),
        "replayed": result["replayed"],
    }
