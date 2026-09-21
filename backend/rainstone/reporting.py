import csv
import io
import statistics
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rainstone.auth import Identity
from rainstone.models import (
    CostLine,
    CostRevision,
    ExecutionAttempt,
    InfrastructureInterval,
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
from rainstone.report_query import ReportQuery

ZERO = Decimal("0")
SORT_FIELDS = {"created_at", "source_id", "tool_id", "state", "runner", "owner", "amount"}


def _revision(session: Session, identity: Identity, requested: str | None) -> CostRevision | None:
    statement = select(CostRevision).where(CostRevision.tenant_id == identity.tenant_id)
    if requested:
        try:
            statement = statement.where(CostRevision.id == uuid.UUID(requested))
        except ValueError as exc:
            raise HTTPException(422, "Invalid calculation revision") from exc
    revision = session.scalar(statement.order_by(CostRevision.created_at.desc(), CostRevision.id.desc()))
    if requested and revision is None:
        raise HTTPException(409, "Calculation revision is no longer available")
    if requested and revision:
        latest_id = session.scalar(
            select(CostRevision.id)
            .where(CostRevision.tenant_id == identity.tenant_id)
            .order_by(CostRevision.created_at.desc(), CostRevision.id.desc())
            .limit(1)
        )
        if latest_id != revision.id:
            raise HTTPException(
                409,
                "This snapshot is stale because report facts changed; refresh to select the latest revision",
            )
    return revision


def validate_snapshot(session: Session, identity: Identity, query: ReportQuery) -> None:
    """Fail before a streaming response starts if its requested snapshot is stale."""
    _revision(session, identity, query.revision)


def _quality(lines: list[CostLine]) -> str:
    if not lines:
        return Quality.in_progress.value
    qualities = {line.quality for line in lines}
    if Quality.partial in qualities:
        return Quality.partial.value
    if Quality.unpriced in qualities:
        return Quality.unpriced.value
    if any(line.amount is None for line in lines):
        return Quality.partial.value
    if Quality.approximate in qualities:
        return Quality.approximate.value
    if qualities == {Quality.known_zero}:
        return Quality.known_zero.value
    return Quality.complete.value


def _line_slices(
    line: CostLine, interval: ResourceInterval, as_of: datetime
) -> list[tuple[datetime, datetime, Decimal | None]]:
    allocations = (line.details or {}).get("allocations") or []
    if allocations:
        amounts = [Decimal(value["amount"]) for value in allocations]
        if line.amount is not None:
            rounded = [value.quantize(Decimal("0.000000000001")) for value in amounts[:-1]]
            amounts = [*rounded, line.amount - sum(rounded, ZERO)]
        return [
            (datetime.fromisoformat(value["start"]), datetime.fromisoformat(value["end"]), amount)
            for value, amount in zip(allocations, amounts, strict=True)
        ]
    if interval.observed_start:
        end = interval.observed_end or as_of
        if end > interval.observed_start:
            return [(interval.observed_start, end, line.amount)]
    return []


def _slice_fraction(start: datetime, end: datetime, query: ReportQuery) -> Decimal:
    lower, upper = query.from_time or start, query.to_time or end
    overlap = max(0.0, (min(end, upper) - max(start, lower)).total_seconds())
    return Decimal(str(overlap)) / Decimal(str((end - start).total_seconds()))


def _authorized_invocations(session: Session, identity: Identity):
    statement = select(Invocation).where(Invocation.tenant_id == identity.tenant_id)
    if not identity.is_admin:
        statement = statement.where(Invocation.owner_id == identity.owner_id)
    return list(session.scalars(statement))


def _workflow_job_ids(session: Session, identity: Identity, query: ReportQuery) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Resolve workflow filters to authorized, ownership-consistent memberships."""
    invocations = _authorized_invocations(session, identity)
    selected = invocations
    if query.invocation_id:
        try:
            requested = uuid.UUID(query.invocation_id)
            selected = [inv for inv in selected if inv.id == requested]
        except ValueError:
            selected = [inv for inv in selected if inv.source_id == query.invocation_id]
    if query.workflow_id:
        selected = [inv for inv in selected if (inv.workflow_id or inv.source_id) == query.workflow_id]
    if query.search:
        term = query.search.casefold()
        selected = [inv for inv in selected if term in (
            f"{inv.source_id} {inv.workflow_id or ''} {inv.workflow_name} {inv.workflow_version or ''}".casefold()
        )]
    invocation_ids = {inv.id for inv in selected}
    if not invocation_ids:
        return set(), set()
    # A malformed cross-owner membership cannot expand a viewer's authorized cohort.
    rows = session.execute(
        select(InvocationJob.job_id, InvocationJob.invocation_id)
        .join(Invocation, InvocationJob.invocation_id == Invocation.id)
        .join(Job, InvocationJob.job_id == Job.id)
        .where(
            InvocationJob.invocation_id.in_(invocation_ids),
            Job.tenant_id == identity.tenant_id,
            Job.owner_id == Invocation.owner_id,
        )
    )
    return {row.job_id for row in rows}, invocation_ids


def _base_records(
    session: Session,
    identity: Identity,
    query: ReportQuery,
    *,
    candidate_offset: int | None = None,
    candidate_limit: int | None = None,
    candidate_count: list[int] | None = None,
) -> tuple[list[dict], CostRevision | None]:
    revision = _revision(session, identity, query.revision)
    workflow_jobs, _ = _workflow_job_ids(session, identity, query)
    statement = (
        select(Job, Owner).join(Owner, Job.owner_id == Owner.id)
        .where(Job.tenant_id == identity.tenant_id)
    )
    if not identity.is_admin:
        statement = statement.where(Job.owner_id == identity.owner_id)
    if query.owner:
        if not identity.is_admin and query.owner != identity.source_id:
            return [], revision
        statement = statement.where(Owner.source_id == query.owner)
    if query.search:
        term = f"%{query.search}%"
        job_match = (
            Job.source_id.ilike(term) | Job.tool_id.ilike(term)
            | func.coalesce(Job.tool_version, "").ilike(term) | Owner.label.ilike(term)
        )
        if workflow_jobs:
            job_match = job_match | Job.id.in_(workflow_jobs)
        statement = statement.where(job_match)
    if query.tool_id:
        statement = statement.where(Job.tool_id == query.tool_id)
    if query.tool_version:
        statement = statement.where(Job.tool_version == query.tool_version)
    if query.state:
        statement = statement.where(Job.state == query.state)
    if query.runner:
        statement = statement.where(Job.runner == query.runner)
    if query.destination:
        statement = statement.where(Job.destination == query.destination)
    if candidate_offset is not None and candidate_limit is not None:
        sort_columns = {
            "created_at": Job.created_at, "source_id": Job.source_id, "tool_id": Job.tool_id,
            "state": Job.state, "runner": Job.runner, "owner": Owner.label,
        }
        column = sort_columns.get(query.sort, Job.created_at)
        order = column.asc().nullslast() if query.direction == "asc" else column.desc().nullslast()
        statement = statement.order_by(order, Job.id).offset(candidate_offset).limit(candidate_limit)
    jobs = session.execute(statement).all()
    if candidate_count is not None:
        candidate_count.append(len(jobs))
    job_ids = [job.id for job, _ in jobs]
    line_map: dict[uuid.UUID, list[tuple[CostLine, ResourceInterval, ExecutionAttempt]]] = defaultdict(list)
    if revision and job_ids:
        rows = session.execute(
            select(CostLine, ResourceInterval, ExecutionAttempt)
            .join(ResourceInterval, CostLine.resource_interval_id == ResourceInterval.id)
            .join(ExecutionAttempt, CostLine.attempt_id == ExecutionAttempt.id)
            .where(
                CostLine.revision_id == revision.id,
                CostLine.basis == query.basis,
                CostLine.job_id.in_(job_ids),
            )
        )
        for line, interval, attempt in rows:
            line_map[line.job_id].append((line, interval, attempt))

    invocation_jobs: set[uuid.UUID] | None = None
    if query.invocation_id or query.workflow_id:
        invocation_jobs = workflow_jobs

    as_of = revision.created_at if revision else datetime.now(UTC)
    records: list[dict] = []
    for job, owner in jobs:
        if invocation_jobs is not None and job.id not in invocation_jobs:
            continue
        pairs = line_map[job.id]
        lines = [pair[0] for pair in pairs]
        quality = _quality(lines)
        known_amounts: list[Decimal] = []
        unattributed = ZERO
        capacities: set[str] = set()
        interval_hit = False
        has_timing = False
        for line, interval, _ in pairs:
            capacities.add(interval.capacity_relationship.value)
            slices = _line_slices(line, interval, as_of)
            has_timing = has_timing or bool(slices)
            if not slices:
                if line.amount is not None:
                    unattributed += line.amount
            for start, end, slice_amount in slices:
                fraction = _slice_fraction(start, end, query)
                if fraction > 0:
                    interval_hit = True
                    if slice_amount is not None:
                        known_amounts.append(slice_amount * min(fraction, Decimal("1")))
        completions = [
            pair[2].tool_finished_at for pair in pairs
            if pair[2].outcome == "ok" and pair[2].tool_finished_at
        ]
        completed_at = max(completions) if completions else None
        if query.mode == "completed":
            if not completed_at or (query.from_time and completed_at < query.from_time) or (query.to_time and completed_at >= query.to_time):
                continue
            known_amounts = [line.amount for line, _, _ in pairs if line.amount is not None]
        elif (query.from_time or query.to_time) and pairs and has_timing and not interval_hit:
            continue
        amount = sum(known_amounts, ZERO) if known_amounts else None
        if any(line.amount is not None for line in lines) and amount is None and not (query.from_time or query.to_time):
            amount = ZERO
        if query.tool_id and job.tool_id != query.tool_id:
            continue
        if query.tool_version and job.tool_version != query.tool_version:
            continue
        if query.state and job.state != query.state:
            continue
        if query.runner and job.runner != query.runner:
            continue
        if query.destination and job.destination != query.destination:
            continue
        if query.capacity and query.capacity not in capacities:
            continue
        if query.quality and quality != query.quality:
            continue
        if query.min_cost is not None and (amount is None or amount < query.min_cost):
            continue
        if query.max_cost is not None and (amount is None or amount > query.max_cost):
            continue
        records.append({
            "id": str(job.id), "source_id": job.source_id, "tool_id": job.tool_id,
            "tool_version": job.tool_version, "owner": owner.label, "owner_id": owner.source_id,
            "state": job.state, "runner": job.runner, "destination": job.destination,
            "created_at": job.created_at, "updated_at": job.updated_at, "amount": amount,
            "currency": "USD", "quality": quality,
            "reason": " | ".join(sorted({line.reason for line in lines})) or "Awaiting cost calculation.",
            "attempt_cost_lines": len(lines), "capacities": sorted(capacities),
            "unattributed_amount": unattributed,
            "temporally_unattributed": bool(pairs and not has_timing) or not pairs,
            "completed_at": completed_at,
            "full_amount": sum((line.amount for line in lines if line.amount is not None), ZERO)
            if any(line.amount is not None for line in lines) else None,
            "job": job, "pairs": pairs,
        })
    return records, revision


def _window(query: ReportQuery, records: list[dict]) -> dict:
    starts = [pair[1].observed_start for record in records for pair in record["pairs"] if pair[1].observed_start]
    ends = [pair[1].observed_end for record in records for pair in record["pairs"] if pair[1].observed_end]
    lower = query.from_time or (min(starts) if starts else None)
    upper = query.to_time or (max(ends) if ends else None)
    return {
        "from": lower.isoformat() if lower else None, "to": upper.isoformat() if upper else None,
        "timezone": query.timezone, "semantics": "[from, to)", "mode": query.mode,
    }


def _meta(session: Session, identity: Identity, query: ReportQuery, revision, records: list[dict]) -> dict:
    priced = [r for r in records if r["amount"] is not None]
    return {
        "basis": query.basis, "currency": "USD",
        "applied_filters": query.model_dump(mode="json", exclude={"limit", "offset", "sort", "direction"}),
        "observation_window": _window(query, records),
        "revision_id": str(revision.id) if revision else None,
        "calculation_version": revision.calculation_version if revision else None,
        "as_of": revision.created_at.isoformat() if revision else None,
        "priced_subtotal": str(sum((r["amount"] for r in priced), ZERO)) if priced else None,
        "coverage": {
            "jobs": len(records), "priced": len(priced),
            "incomplete": sum(r["quality"] in {"partial", "unpriced", "in_progress"} for r in records),
            "known_zero": sum(r["quality"] == "known_zero" for r in records),
            "temporally_unattributed": sum(r["temporally_unattributed"] for r in records),
        },
    }


def _public(record: dict) -> dict:
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in record.items() if key not in {"job", "pairs", "full_amount"}
    }


def summary(session: Session, identity: Identity, query: ReportQuery) -> dict:
    records, revision = _base_records(session, identity, query)
    meta = _meta(session, identity, query, revision, records)
    failed = sum((r["amount"] or ZERO for r in records if r["state"] in {"error", "failed"}), ZERO)
    retried = sum((r["amount"] or ZERO for r in records if len({p[2].id for p in r["pairs"]}) > 1), ZERO)
    tenant = session.get(Tenant, identity.tenant_id)
    infra = infrastructure(session, identity, query) if identity.is_admin else None
    return {
        **meta, "amount": meta["priced_subtotal"], "job_count": len(records),
        "priced_job_count": meta["coverage"]["priced"],
        "unpriced_job_count": meta["coverage"]["incomplete"],
        "known_zero_job_count": meta["coverage"]["known_zero"],
        "failed_spend": str(failed), "retried_spend": str(retried),
        "baseline_infrastructure_amount": infra["amount"] if infra else None,
        "can_view_infrastructure": identity.is_admin,
        "demo": bool((tenant.capabilities or {}).get("demo", True)),
    }


def list_jobs(session: Session, identity: Identity, query: ReportQuery, paginate: bool = True) -> dict:
    records, revision = _base_records(session, identity, query)
    if query.sort not in SORT_FIELDS:
        raise HTTPException(422, f"Unsupported sort field: {query.sort}")
    known = [r for r in records if r[query.sort] is not None]
    missing = [r for r in records if r[query.sort] is None]
    known.sort(key=lambda r: (r[query.sort], r["id"]), reverse=query.direction == "desc")
    records = known + sorted(missing, key=lambda r: r["id"])
    page = records[query.offset:query.offset + query.limit] if paginate else records
    return {
        "items": [_public(r) for r in page], "total": len(records),
        "limit": query.limit, "offset": query.offset,
        "meta": _meta(session, identity, query, revision, records),
    }


def job_detail(session: Session, identity: Identity, job_id: uuid.UUID, query: ReportQuery) -> dict | None:
    unrestricted = query.model_copy(update={
        "search": None, "tool_id": None, "tool_version": None, "invocation_id": None,
        "min_cost": None, "max_cost": None, "quality": None,
    })
    records, revision = _base_records(session, identity, unrestricted)
    record = next((r for r in records if r["id"] == str(job_id)), None)
    if not record:
        return None
    full_records, _ = _base_records(session, identity, unrestricted.model_copy(update={"from_time": None, "to_time": None}))
    full = next(r for r in full_records if r["id"] == str(job_id))
    grouped: dict[uuid.UUID, list[tuple]] = defaultdict(list)
    for pair in record["pairs"]:
        grouped[pair[2].id].append(pair)
    attempts = []
    for attempt_id, pairs in grouped.items():
        attempt, interval = pairs[0][2], pairs[0][1]
        attempts.append({
            "id": str(attempt_id), "source_attempt_id": attempt.source_attempt_id,
            "runner": attempt.runner, "outcome": attempt.outcome,
            "tool_started_at": attempt.tool_started_at, "tool_finished_at": attempt.tool_finished_at,
            "resource_started_at": interval.observed_start, "resource_finished_at": interval.observed_end,
            "requested_vcpu": str(interval.requested_vcpu) if interval.requested_vcpu is not None else None,
            "requested_memory_mib": str(interval.requested_memory_mib) if interval.requested_memory_mib is not None else None,
            "provisioned": interval.facts, "machine_type": interval.machine_type,
            "capacity_relationship": interval.capacity_relationship.value,
            "amount": str(sum((p[0].amount for p in pairs if p[0].amount is not None), ZERO))
            if any(p[0].amount is not None for p in pairs) else None,
            "quality": _quality([p[0] for p in pairs]),
            "reason": " | ".join(sorted({p[0].reason for p in pairs})),
            "provenance": [p[0].details for p in pairs],
        })
    result = _public(record)
    result.update({
        "interval_amount": result["amount"],
        "full_job_amount": str(full["amount"]) if full["amount"] is not None else None,
        "basis": query.basis, "attempts": attempts,
        "revision_id": str(revision.id) if revision else None,
    })
    result["cost"] = {
        "basis": query.basis, "amount": result["interval_amount"], "currency": "USD",
        "quality": result["quality"], "reason": result["reason"], "attempts": attempts,
    }
    return result


def tools(session: Session, identity: Identity, query: ReportQuery) -> dict:
    records, revision = _base_records(session, identity, query)
    groups: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    for record in records:
        groups[(record["tool_id"], record["tool_version"])].append(record)
    items = []
    for (tool_id, version), rows in groups.items():
        known = [r["amount"] for r in rows if r["amount"] is not None]
        complete_rows = [
            r for r in rows
            if r["state"] == "ok" and r["full_amount"] is not None
            and r["quality"] not in {"partial", "unpriced", "in_progress"}
        ]
        values = sorted(r["full_amount"] for r in complete_rows)
        if values:
            position = Decimal("0.95") * Decimal(len(values) - 1)
            lower = int(position)
            fraction = position - lower
            p95 = values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * fraction
        else:
            p95 = None
        items.append({
            "tool_id": tool_id, "tool_version": version, "job_count": len(rows),
            "amount": str(sum(known, ZERO)) if known else None, "priced_count": len(known),
            "incomplete_count": sum(r["quality"] in {"partial", "unpriced", "in_progress"} for r in rows),
            "statistics": {
                "cohort": "complete successful jobs", "sample_count": len(values),
                "excluded_count": len(rows) - len(values),
                "mean": str(statistics.mean(values)) if values else None,
                "median": str(statistics.median(values)) if values else None,
                "p95": str(p95) if p95 is not None else None,
                "method": "continuous linear interpolation (R-7)",
                "approximate": any(r["quality"] == "approximate" for r in complete_rows),
            },
        })
    items.sort(key=lambda x: (
        x["amount"] is None, -(Decimal(x["amount"]) if x["amount"] else ZERO),
        x["tool_id"], x["tool_version"] or "",
    ))
    return {
        "items": items[query.offset:query.offset + query.limit], "total": len(items),
        "limit": query.limit, "offset": query.offset,
        "meta": _meta(session, identity, query, revision, records),
    }


def invocations(session: Session, identity: Identity, query: ReportQuery, roots_only: bool = True) -> dict:
    records, revision = _base_records(session, identity, query)
    by_id = {uuid.UUID(r["id"]): r for r in records}
    permitted = _authorized_invocations(session, identity)
    _, selected_ids = _workflow_job_ids(session, identity, query)
    constrained = bool(query.invocation_id or query.workflow_id)
    items = []
    for inv in sorted(permitted, key=lambda value: value.created_at, reverse=True):
        if roots_only and inv.parent_id is not None:
            continue
        if constrained and inv.id not in selected_ids:
            continue
        job_ids = set(session.scalars(
            select(InvocationJob.job_id)
            .join(Job, InvocationJob.job_id == Job.id)
            .where(InvocationJob.invocation_id == inv.id, Job.owner_id == inv.owner_id)
        ))
        rows = [by_id[job_id] for job_id in job_ids if job_id in by_id]
        search_hit = (query.search or "").casefold() in (
            f"{inv.source_id} {inv.workflow_name} {inv.workflow_version or ''}".casefold()
        )
        if query.search and not search_hit and not rows:
            continue
        known = [r["amount"] for r in rows if r["amount"] is not None]
        items.append({
            "id": str(inv.id), "source_id": inv.source_id,
            "workflow_id": inv.workflow_id or inv.source_id,
            "workflow_name": inv.workflow_name, "workflow_version": inv.workflow_version,
            "parent_id": str(inv.parent_id) if inv.parent_id else None, "state": inv.state,
            "job_count": len(rows), "amount": str(sum(known, ZERO)) if known else None,
            "currency": "USD",
            "unpriced_job_count": sum(r["quality"] in {"partial", "unpriced", "in_progress"} for r in rows),
            "reused_job_count": sum(bool(r["job"].copied_from_source_id) for r in rows),
        })
    return {
        "items": items[query.offset:query.offset + query.limit], "total": len(items),
        "limit": query.limit, "offset": query.offset,
        "meta": _meta(session, identity, query, revision, records),
    }


def invocation_detail(
    session: Session, identity: Identity, invocation_id: uuid.UUID, query: ReportQuery
) -> dict | None:
    result = invocations(session, identity, query, roots_only=False)
    item = next((value for value in result["items"] if value["id"] == str(invocation_id)), None)
    if not item:
        return None
    related = invocations(
        session, identity,
        query.model_copy(update={"invocation_id": None, "workflow_id": None, "search": None}),
        roots_only=False,
    )
    memberships = session.scalars(
        select(InvocationJob).where(InvocationJob.invocation_id == invocation_id)
        .order_by(InvocationJob.step_key)
    ).all()
    jobs = list_jobs(
        session, identity,
        query.model_copy(update={"invocation_id": str(invocation_id), "limit": 200}),
        paginate=False,
    )["items"]
    by_id = {job["id"]: job for job in jobs}
    item["steps"] = [{
        "step_key": membership.step_key, "relationship": membership.relationship,
        "job": by_id.get(str(membership.job_id)),
    } for membership in memberships]
    item["children"] = [
        value for value in related["items"] if value["parent_id"] == str(invocation_id)
    ]
    item["meta"] = result["meta"]
    return item


def daily(session: Session, identity: Identity, query: ReportQuery) -> dict:
    records, revision = _base_records(session, identity, query)
    timezone = ZoneInfo(query.timezone)
    buckets: dict[str, dict] = defaultdict(lambda: {
        "amount": ZERO, "job_ids": set(), "incomplete_ids": set(), "provisional": False,
        "by_runner": defaultdict(Decimal), "by_owner": defaultdict(Decimal),
        "by_tool": defaultdict(Decimal),
    })
    as_of = revision.created_at if revision else datetime.now(UTC)
    for record in records:
        if query.mode == "completed":
            completed_at = record["completed_at"]
            if not completed_at:
                continue
            key = completed_at.astimezone(timezone).date().isoformat()
            bucket = buckets[key]
            bucket["job_ids"].add(record["id"])
            if record["amount"] is None:
                bucket["incomplete_ids"].add(record["id"])
                continue
            amount = record["amount"]
            bucket["amount"] += amount
            bucket["by_runner"][record["runner"] or "unknown"] += amount
            bucket["by_owner"][record["owner"]] += amount
            bucket["by_tool"][f"{record['tool_id']}@{record['tool_version'] or ''}"] += amount
            continue
        for line, interval, _ in record["pairs"]:
            for start, end, slice_amount in _line_slices(line, interval, as_of):
                total_seconds = Decimal(str((end - start).total_seconds()))
                cursor = max(start, query.from_time or start)
                limit = min(end, query.to_time or end)
                while cursor < limit:
                    local = cursor.astimezone(timezone)
                    next_day = datetime.combine(
                        local.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone
                    ).astimezone(UTC)
                    chunk_end = min(limit, next_day)
                    key = local.date().isoformat()
                    bucket = buckets[key]
                    bucket["job_ids"].add(record["id"])
                    bucket["provisional"] = bucket["provisional"] or interval.observed_end is None
                    if slice_amount is None:
                        bucket["incomplete_ids"].add(record["id"])
                        cursor = chunk_end
                        continue
                    amount = slice_amount * Decimal(str((chunk_end - cursor).total_seconds())) / total_seconds
                    bucket["amount"] += amount
                    bucket["by_runner"][record["runner"] or "unknown"] += amount
                    bucket["by_owner"][record["owner"]] += amount
                    bucket["by_tool"][f"{record['tool_id']}@{record['tool_version'] or ''}"] += amount
                    cursor = chunk_end
    items = [{
        "date": day, "amount": str(data["amount"]), "currency": "USD",
        "job_count": len(data["job_ids"]), "incomplete_count": len(data["incomplete_ids"]),
        "provisional": data["provisional"],
        "by_runner": {key: str(value) for key, value in data["by_runner"].items()},
        "by_owner": {key: str(value) for key, value in data["by_owner"].items()},
        "by_tool": {key: str(value) for key, value in data["by_tool"].items()},
    } for day, data in sorted(buckets.items())]
    return {
        "items": items,
        "label": "Cost of jobs completed per day" if query.mode == "completed" else "Cost accrued per day",
        "temporally_unattributed_count": sum(r["temporally_unattributed"] for r in records),
        "temporally_unattributed_subtotal": str(sum((r["unattributed_amount"] for r in records), ZERO)),
        "meta": _meta(session, identity, query, revision, records),
    }


def users(session: Session, identity: Identity, query: ReportQuery) -> dict:
    if not identity.is_admin:
        raise HTTPException(403, "Administrator scope required")
    records, revision = _base_records(session, identity, query)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        groups[(record["owner_id"], record["owner"])].append(record)
    items = []
    for (owner_id, label), rows in groups.items():
        known = [row["amount"] for row in rows if row["amount"] is not None]
        items.append({
            "owner_id": owner_id, "label": label, "job_count": len(rows),
            "amount": str(sum(known, ZERO)) if known else None,
            "priced_count": len(known), "incomplete_count": len(rows) - len(known),
        })
    return {
        "items": sorted(items, key=lambda item: item["label"])[query.offset:query.offset + query.limit],
        "total": len(items), "limit": query.limit, "offset": query.offset,
        "meta": _meta(session, identity, query, revision, records),
    }


def infrastructure(session: Session, identity: Identity, query: ReportQuery) -> dict:
    if not identity.is_admin:
        raise HTTPException(403, "Infrastructure reporting requires administrator scope")
    rows = session.scalars(
        select(InfrastructureInterval)
        .where(InfrastructureInterval.tenant_id == identity.tenant_id)
        .order_by(InfrastructureInterval.observed_start)
    ).all()
    items = []
    for row in rows:
        start, end = row.observed_start, row.observed_end
        overlap_start, overlap_end = max(start, query.from_time or start), min(end, query.to_time or end)
        fraction = max(0.0, (overlap_end - overlap_start).total_seconds()) / (end - start).total_seconds()
        if fraction > 0:
            items.append({
                "id": str(row.id), "resource_uid": row.resource_uid,
                "machine_type": row.machine_type, "region": row.region,
                "observed_start": start, "observed_end": end,
                "amount": str(row.amount * Decimal(str(fraction))),
                "currency": row.currency, "quality": row.quality.value,
            })
    return {
        "items": items,
        "amount": str(sum((Decimal(item["amount"]) for item in items), ZERO)) if items else None,
        "currency": "USD", "scope": "Whole baseline resources; job filters do not apportion this total.",
        "allocation_supported": False,
        "allocation_reason": "T2D allocation is unavailable without a valid component policy.",
        "observation_window": {
            "from": (query.from_time or (min((row.observed_start for row in rows), default=None))).isoformat()
            if (query.from_time or rows) else None,
            "to": (query.to_time or (max((row.observed_end for row in rows), default=None))).isoformat()
            if (query.to_time or rows) else None,
            "timezone": query.timezone, "semantics": "[from, to)", "mode": query.mode,
        },
    }


def freshness(session: Session, identity: Identity) -> dict:
    rows = session.scalars(
        select(IngestionState).where(IngestionState.tenant_id == identity.tenant_id)
        .order_by(IngestionState.source)
    ).all()
    now = datetime.now(UTC)
    sources = []
    for row in rows:
        status = row.status
        if status == "healthy" and row.last_success_at and now - row.last_success_at > timedelta(hours=24):
            status = "stale"
        sources.append({
            "source": row.source, "status": status, "last_success_at": row.last_success_at,
            "cursor": row.cursor, "error": row.error,
        })
    price = session.execute(
        select(PriceVersion.catalog_id, PriceVersion.observed_at)
        .order_by(PriceVersion.observed_at.desc()).limit(1)
    ).first()
    if price:
        sources.append({
            "source": "price_catalog", "status": "historical_snapshot",
            "last_success_at": price.observed_at,
            "cursor": {"catalog_id": price.catalog_id}, "error": None,
        })
    statuses = {source["status"] for source in sources}
    overall = "failed" if "failed" in statuses else (
        "partial" if statuses - {"healthy", "historical_snapshot"} else "healthy"
    )
    return {"sources": sources, "overall_status": overall}


def export_csv(session: Session, identity: Identity, query: ReportQuery):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "job_id", "source_id", "tool_id", "tool_version", "owner", "state", "runner",
        "destination", "basis", "currency", "time_semantics", "amount_exact", "quality",
        "reason", "revision_id",
    ])
    yield output.getvalue()
    output.seek(0)
    output.truncate(0)
    def safe(value):
        text = "" if value is None else str(value)
        return "'" + text if text[:1] in {"=", "+", "-", "@"} else text
    batch_size = 500
    candidate_offset = 0
    export_query = query.model_copy(update={"offset": 0, "limit": 200})
    while True:
        # Cost sorting depends on calculated lines, so preserve its global order as a compatibility fallback.
        if query.sort == "amount":
            result = list_jobs(session, identity, export_query, paginate=False)
            records = result["items"]
            revision_id = result["meta"]["revision_id"]
            exhausted = True
        else:
            raw_count: list[int] = []
            batch, revision = _base_records(
                session, identity, export_query,
                candidate_offset=candidate_offset, candidate_limit=batch_size,
                candidate_count=raw_count,
            )
            records = [_public(record) for record in batch]
            revision_id = str(revision.id) if revision else None
            exhausted = raw_count[0] < batch_size
        for row in records:
            writer.writerow([
                *[safe(row.get(key)) for key in (
                    "id", "source_id", "tool_id", "tool_version", "owner", "state",
                    "runner", "destination",
                )],
                query.basis, "USD",
                "accrued [from,to)" if query.mode == "accrued" else "completed in range",
                safe(row["amount"]), row["quality"], safe(row["reason"]), revision_id,
            ])
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)
        if exhausted:
            break
        candidate_offset += batch_size
