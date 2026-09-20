import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

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
    Owner,
    PriceVersion,
    Quality,
    ResourceInterval,
    Tenant,
)

NAMESPACE = uuid.UUID("22fcfbaa-52d7-4bce-af8f-452d26f79cc8")


def stable_id(*parts: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, ":".join(parts))


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _set(obj: object, data: dict, fields: tuple[str, ...]) -> None:
    for field in fields:
        if field in data:
            setattr(obj, field, data[field])


def ingest_fixture(session: Session, path: Path) -> dict[str, int | str]:
    data = json.loads(path.read_text())
    tenant_data = data["tenant"]
    tenant_id = stable_id("tenant", tenant_data["slug"])
    payload_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    prior = session.scalar(
        select(IngestionEvent).where(
            IngestionEvent.tenant_id == tenant_id,
            IngestionEvent.source == "fixture",
            IngestionEvent.idempotency_key == data["fixture_id"],
        )
    )

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        tenant = Tenant(id=tenant_id, slug=tenant_data["slug"], display_name=tenant_data["display_name"])
        session.add(tenant)
    _set(tenant, tenant_data, ("display_name", "source_version", "base_url", "capabilities"))
    tenant.synced_at = parse_time(data["observed_at"])

    owner_ids: dict[str, uuid.UUID] = {}
    for item in data["owners"]:
        owner_id = stable_id(str(tenant_id), "owner", item["source_id"])
        owner_ids[item["source_id"]] = owner_id
        obj = session.get(Owner, owner_id)
        if obj is None:
            obj = Owner(id=owner_id, tenant_id=tenant_id, source_id=item["source_id"])
            session.add(obj)
        _set(obj, item, ("label", "is_admin"))

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

    job_ids: dict[str, uuid.UUID] = {}
    attempt_ids: dict[str, uuid.UUID] = {}
    for item in data["jobs"]:
        job_id = stable_id(str(tenant_id), "job", item["source_id"])
        job_ids[item["source_id"]] = job_id
        job = session.get(Job, job_id)
        if job is None:
            job = Job(id=job_id, tenant_id=tenant_id, source_id=item["source_id"])
            session.add(job)
        job.owner_id = owner_ids[item["owner_source_id"]]
        _set(
            job, item, ("tool_id", "tool_version", "state", "runner", "destination", "copied_from_source_id")
        )
        job.created_at = parse_time(item["created_at"])
        job.updated_at = parse_time(item["updated_at"])
        for attempt_data in item["attempts"]:
            attempt_id = stable_id(str(job_id), "attempt", attempt_data["source_attempt_id"])
            attempt_ids[f"{item['source_id']}:{attempt_data['source_attempt_id']}"] = attempt_id
            attempt = session.get(ExecutionAttempt, attempt_id)
            if attempt is None:
                attempt = ExecutionAttempt(
                    id=attempt_id, job_id=job_id, source_attempt_id=attempt_data["source_attempt_id"]
                )
                session.add(attempt)
            _set(attempt, attempt_data, ("runner", "external_id", "outcome"))
            attempt.tool_started_at = parse_time(attempt_data.get("tool_started_at"))
            attempt.tool_finished_at = parse_time(attempt_data.get("tool_finished_at"))
            for interval_data in attempt_data["resource_intervals"]:
                interval_id = stable_id(str(attempt_id), "interval", interval_data["source_interval_id"])
                interval = session.get(ResourceInterval, interval_id)
                if interval is None:
                    interval = ResourceInterval(
                        id=interval_id,
                        attempt_id=attempt_id,
                        source_interval_id=interval_data["source_interval_id"],
                    )
                    session.add(interval)
                _set(
                    interval,
                    interval_data,
                    (
                        "resource_uid",
                        "provider",
                        "region",
                        "machine_type",
                        "purchase_model",
                        "timing_method",
                        "facts",
                    ),
                )
                interval.capacity_relationship = CapacityRelationship(interval_data["capacity_relationship"])
                interval.observed_start = parse_time(interval_data.get("observed_start"))
                interval.observed_end = parse_time(interval_data.get("observed_end"))
                interval.requested_vcpu = (
                    Decimal(str(interval_data["requested_vcpu"]))
                    if interval_data.get("requested_vcpu") is not None
                    else None
                )
                interval.requested_memory_mib = (
                    Decimal(str(interval_data["requested_memory_mib"]))
                    if interval_data.get("requested_memory_mib") is not None
                    else None
                )

    invocation_ids = {
        item["source_id"]: stable_id(str(tenant_id), "invocation", item["source_id"])
        for item in data["invocations"]
    }
    for item in data["invocations"]:
        invocation = session.get(Invocation, invocation_ids[item["source_id"]])
        if invocation is None:
            invocation = Invocation(
                id=invocation_ids[item["source_id"]], tenant_id=tenant_id, source_id=item["source_id"]
            )
            session.add(invocation)
        invocation.owner_id = owner_ids[item["owner_source_id"]]
        invocation.parent_id = invocation_ids.get(item.get("parent_source_id"))
        _set(invocation, item, ("workflow_name", "workflow_version", "state"))
        invocation.created_at = parse_time(item["created_at"])
        for membership in item["jobs"]:
            key = {
                "invocation_id": invocation.id,
                "job_id": job_ids[membership["job_source_id"]],
                "step_key": membership["step_key"],
            }
            obj = session.get(InvocationJob, tuple(key.values()))
            if obj is None:
                session.add(InvocationJob(**key, relationship=membership["relationship"]))

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

    now = datetime.now(UTC)
    if prior is None:
        session.add(
            IngestionEvent(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                source="fixture",
                idempotency_key=data["fixture_id"],
                payload_digest=payload_digest,
                observed_at=parse_time(data["observed_at"]),
                ingested_at=now,
            )
        )
    else:
        prior.payload_digest = payload_digest
        prior.ingested_at = now
    state_id = stable_id(str(tenant_id), "state", "fixture")
    state = session.get(IngestionState, state_id)
    if state is None:
        state = IngestionState(id=state_id, tenant_id=tenant_id, source="fixture")
        session.add(state)
    state.cursor = {"fixture_id": data["fixture_id"], "payload_digest": payload_digest}
    state.status = "healthy"
    state.last_success_at = now
    state.error = None
    session.flush()
    revision = calculate_tenant(session, tenant_id)
    session.commit()
    return {
        "tenant": tenant_data["slug"],
        "jobs": len(data["jobs"]),
        "revision": str(revision.id),
        "replayed": prior is not None,
    }
