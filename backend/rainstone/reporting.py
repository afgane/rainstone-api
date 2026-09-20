import csv
import io
import math
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
    return revision


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


def _fraction(interval: ResourceInterval, query: ReportQuery, as_of: datetime) -> Decimal | None:
    start = interval.observed_start
    end = interval.observed_end or (as_of if start else None)
    if not start or not end or end <= start:
        return None
    if not query.from_time and not query.to_time:
        return Decimal("1")
    lower, upper = query.from_time or start, query.to_time or end
    overlap = max(0.0, (min(end, upper) - max(start, lower)).total_seconds())
    return Decimal(str(overlap)) / Decimal(str((end - start).total_seconds()))


def _base_records(session: Session, identity: Identity, query: ReportQuery) -> tuple[list[dict], CostRevision | None]:
    revision = _revision(session, identity, query.revision)
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
        statement = statement.where(
            Job.source_id.ilike(term) | Job.tool_id.ilike(term)
            | func.coalesce(Job.tool_version, "").ilike(term) | Owner.label.ilike(term)
        )
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
    jobs = session.execute(statement).all()
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
    if query.invocation_id:
        try:
            inv_id = uuid.UUID(query.invocation_id)
        except ValueError:
            inv_id = session.scalar(
                select(Invocation.id).where(
                    Invocation.source_id == query.invocation_id,
                    Invocation.tenant_id == identity.tenant_id,
                )
            )
        invocation_jobs = (
            set(session.scalars(select(InvocationJob.job_id).where(InvocationJob.invocation_id == inv_id)))
            if inv_id else set()
        )

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
        for line, interval, _ in pairs:
            capacities.add(interval.capacity_relationship.value)
            if line.amount is None:
                continue
            fraction = _fraction(interval, query, as_of)
            if fraction is None:
                unattributed += line.amount
            elif fraction > 0:
                interval_hit = True
                # This distributes any minimum-charge uplift once across the observed lifetime.
                known_amounts.append(line.amount * min(fraction, Decimal("1")))
        if query.mode == "completed":
            completions = [pair[2].tool_finished_at for pair in pairs if pair[2].outcome == "ok" and pair[2].tool_finished_at]
            completed_at = max(completions) if completions else None
            if not completed_at or (query.from_time and completed_at < query.from_time) or (query.to_time and completed_at >= query.to_time):
                continue
            known_amounts = [line.amount for line, _, _ in pairs if line.amount is not None]
            interval_hit = bool(known_amounts)
        elif (query.from_time or query.to_time) and pairs and not interval_hit and unattributed == ZERO:
            continue
        elif (query.from_time or query.to_time) and not pairs:
            if (query.from_time and job.created_at < query.from_time) or (query.to_time and job.created_at >= query.to_time):
                continue
        amount = sum(known_amounts, ZERO) if known_amounts else None
        if any(line.amount is not None for line in lines) and amount is None and not (query.from_time or query.to_time):
            amount = ZERO
        term = (query.search or "").casefold()
        searchable = " ".join((job.source_id, job.tool_id, job.tool_version or "", owner.label)).casefold()
        if term and term not in searchable:
            continue
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
            "unattributed_amount": unattributed, "job": job, "pairs": pairs,
        })
    return records, revision


def _window(session: Session, identity: Identity, query: ReportQuery) -> dict:
    bounds = session.execute(
        select(func.min(ResourceInterval.observed_start), func.max(ResourceInterval.observed_end))
        .join(ExecutionAttempt, ResourceInterval.attempt_id == ExecutionAttempt.id)
        .join(Job, ExecutionAttempt.job_id == Job.id)
        .where(Job.tenant_id == identity.tenant_id)
    ).one()
    lower, upper = query.from_time or bounds[0], query.to_time or bounds[1]
    return {
        "from": lower.isoformat() if lower else None, "to": upper.isoformat() if upper else None,
        "timezone": query.timezone, "semantics": "[from, to)", "mode": query.mode,
    }


def _meta(session: Session, identity: Identity, query: ReportQuery, revision, records: list[dict]) -> dict:
    priced = [r for r in records if r["amount"] is not None]
    return {
        "basis": query.basis, "currency": "USD",
        "applied_filters": query.model_dump(mode="json", exclude={"limit", "offset", "sort", "direction"}),
        "observation_window": _window(session, identity, query),
        "revision_id": str(revision.id) if revision else None,
        "calculation_version": revision.calculation_version if revision else None,
        "as_of": revision.created_at.isoformat() if revision else None,
        "priced_subtotal": str(sum((r["amount"] for r in priced), ZERO)) if priced else None,
        "coverage": {
            "jobs": len(records), "priced": len(priced),
            "incomplete": sum(r["quality"] in {"partial", "unpriced", "in_progress"} for r in records),
            "known_zero": sum(r["quality"] == "known_zero" for r in records),
            "temporally_unattributed": sum(r["unattributed_amount"] != 0 for r in records),
        },
    }


def _public(record: dict) -> dict:
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in record.items() if key not in {"job", "pairs"}
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
        "interval_amount": result.pop("amount"),
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
        values = sorted(r["amount"] for r in rows if r["state"] == "ok" and r["amount"] is not None)
        p95 = values[math.ceil(.95 * len(values)) - 1] if values else None
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
                "method": "nearest-rank exact", "approximate": False,
            },
        })
    items.sort(key=lambda x: (
        x["amount"] is None, -(Decimal(x["amount"]) if x["amount"] else ZERO),
        x["tool_id"], x["tool_version"] or "",
    ))
    return {"items": items, "total": len(items), "meta": _meta(session, identity, query, revision, records)}


def invocations(session: Session, identity: Identity, query: ReportQuery, roots_only: bool = True) -> dict:
    records, revision = _base_records(session, identity, query.model_copy(update={"invocation_id": None}))
    by_id = {uuid.UUID(r["id"]): r for r in records}
    statement = select(Invocation).where(Invocation.tenant_id == identity.tenant_id)
    if not identity.is_admin:
        statement = statement.where(Invocation.owner_id == identity.owner_id)
    if roots_only:
        statement = statement.where(Invocation.parent_id.is_(None))
    items = []
    for inv in session.scalars(statement.order_by(Invocation.created_at.desc())):
        job_ids = set(session.scalars(
            select(InvocationJob.job_id).where(InvocationJob.invocation_id == inv.id)
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
    return {"items": items, "total": len(items), "meta": _meta(session, identity, query, revision, records)}


def invocation_detail(
    session: Session, identity: Identity, invocation_id: uuid.UUID, query: ReportQuery
) -> dict | None:
    result = invocations(session, identity, query, roots_only=False)
    item = next((value for value in result["items"] if value["id"] == str(invocation_id)), None)
    if not item:
        return None
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
    item["children"] = [value for value in result["items"] if value["parent_id"] == str(invocation_id)]
    item["meta"] = result["meta"]
    return item


def daily(session: Session, identity: Identity, query: ReportQuery) -> dict:
    records, revision = _base_records(session, identity, query)
    timezone = ZoneInfo(query.timezone)
    buckets: dict[str, dict] = defaultdict(lambda: {
        "amount": ZERO, "job_ids": set(), "provisional": False,
        "by_runner": defaultdict(Decimal), "by_owner": defaultdict(Decimal),
        "by_tool": defaultdict(Decimal),
    })
    as_of = revision.created_at if revision else datetime.now(UTC)
    for record in records:
        for line, interval, _ in record["pairs"]:
            if line.amount is None or not interval.observed_start:
                continue
            end = interval.observed_end or as_of
            total_seconds = Decimal(str((end - interval.observed_start).total_seconds()))
            if total_seconds <= 0:
                continue
            cursor = max(interval.observed_start, query.from_time or interval.observed_start)
            limit = min(end, query.to_time or end)
            while cursor < limit:
                local = cursor.astimezone(timezone)
                next_day = datetime.combine(
                    local.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone
                ).astimezone(UTC)
                chunk_end = min(limit, next_day)
                amount = line.amount * Decimal(str((chunk_end - cursor).total_seconds())) / total_seconds
                key = local.date().isoformat()
                bucket = buckets[key]
                bucket["amount"] += amount
                bucket["job_ids"].add(record["id"])
                bucket["provisional"] = bucket["provisional"] or interval.observed_end is None
                bucket["by_runner"][record["runner"] or "unknown"] += amount
                bucket["by_owner"][record["owner"]] += amount
                bucket["by_tool"][f"{record['tool_id']}@{record['tool_version'] or ''}"] += amount
                cursor = chunk_end
    items = [{
        "date": day, "amount": str(data["amount"]), "currency": "USD",
        "job_count": len(data["job_ids"]), "provisional": data["provisional"],
        "by_runner": {key: str(value) for key, value in data["by_runner"].items()},
        "by_owner": {key: str(value) for key, value in data["by_owner"].items()},
        "by_tool": {key: str(value) for key, value in data["by_tool"].items()},
    } for day, data in sorted(buckets.items())]
    return {
        "items": items,
        "temporally_unattributed_count": sum(r["unattributed_amount"] != 0 for r in records),
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
        "items": sorted(items, key=lambda item: item["label"]), "total": len(items),
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
        "observation_window": _window(session, identity, query),
    }


def freshness(session: Session, identity: Identity) -> dict:
    rows = session.scalars(
        select(IngestionState).where(IngestionState.tenant_id == identity.tenant_id)
        .order_by(IngestionState.source)
    ).all()
    sources = [{
        "source": row.source, "status": row.status, "last_success_at": row.last_success_at,
        "cursor": row.cursor, "error": row.error,
    } for row in rows]
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
    overall = "failed" if any(source["status"] == "failed" for source in sources) else "healthy"
    return {"sources": sources, "overall_status": overall}


def export_csv(session: Session, identity: Identity, query: ReportQuery):
    result = list_jobs(session, identity, query, paginate=False)
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
    for row in result["items"]:
        writer.writerow([
            *[safe(row.get(key)) for key in (
                "id", "source_id", "tool_id", "tool_version", "owner", "state",
                "runner", "destination",
            )],
            query.basis, "USD",
            "accrued [from,to)" if query.mode == "accrued" else "completed in range",
            safe(row["amount"]), row["quality"], safe(row["reason"]),
            result["meta"]["revision_id"],
        ])
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)
