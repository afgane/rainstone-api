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

from rainstone.adapters.contracts import GALAXY_RECORD_ATTEMPT_ID
from rainstone.auth import Identity
from rainstone.costing import current_generation
from rainstone.models import (
    CostLine,
    CostRevision,
    ExecutionAttempt,
    InfrastructureInterval,
    IngestionState,
    Invocation,
    InvocationJob,
    Job,
    LifetimeAttempt,
    ObservationGap,
    Owner,
    PriceVersion,
    Quality,
    ResourceLifetime,
    Tenant,
)
from rainstone.report_query import ReportQuery

ZERO = Decimal("0")
SORT_FIELDS = {"created_at", "source_id", "tool_id", "state", "runner", "owner", "amount"}
RUNNING_STATES = {"new", "queued", "running", "paused", "resubmitted"}
# Queued work can already hold provider capacity, so it may be accruing cost.
EXECUTING_STATES = {"queued", "running", "resubmitted"}
UNSTARTED_STATES = {"new", "paused"}
# Record-level qualities beyond the stored line qualities: a job without cost
# lines is unavailable when it finished and not started when it never ran.
UNAVAILABLE = "unavailable"
NOT_STARTED = "not_started"
INCOMPLETE_QUALITIES = {
    Quality.partial.value, Quality.unpriced.value, Quality.in_progress.value,
    UNAVAILABLE, NOT_STARTED,
}


def tool_display_name(tool_id: str) -> str:
    """A readable name derived from the full tool identity.

    Galaxy's database records the tool ID, not its display name, so this is an
    honest fallback rather than the tool's own label. The full identity is
    always carried alongside it, because two different tools can share a short
    name.
    """
    if "/" not in tool_id:
        return tool_id.replace("_", " ")
    parts = [part for part in tool_id.split("/") if part]
    # Tool Shed IDs end with `<repo>/<tool>/<version>`; the tool segment reads
    # best, and a repeated segment adds nothing.
    name = parts[-2] if len(parts) >= 2 else parts[-1]
    return name.replace("_", " ")


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
    if revision and revision.facts_generation != current_generation(session, identity.tenant_id):
        raise HTTPException(
            409,
            "This snapshot is stale because reporting facts changed; refresh to recalculate costs",
        )
    return revision


def validate_snapshot(session: Session, identity: Identity, query: ReportQuery) -> None:
    """Fail before a streaming response starts if its requested snapshot is stale."""
    _revision(session, identity, query.revision)


def _quality(lines: list[CostLine], state: str | None = None) -> str:
    if not lines:
        if state in EXECUTING_STATES:
            return Quality.in_progress.value
        return NOT_STARTED if state in UNSTARTED_STATES else UNAVAILABLE
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


def _missing_evidence_reason(job: Job, attempts: list[ExecutionAttempt]) -> str:
    if job.state in EXECUTING_STATES:
        return "Awaiting execution evidence for work that is still running."
    if job.state == "paused":
        return "Paused before running; no execution was recorded."
    if job.state == "new":
        return "Not started yet; no execution was recorded."
    if any(attempt.tool_started_at for attempt in attempts):
        return (
            "Galaxy recorded when this ran but no evidence of where it ran was collected, so its "
            "cost is unavailable. Recalculating alone will not recover it."
        )
    return (
        "No execution evidence was collected for this job, so its cost is unavailable. "
        "Recalculating alone will not recover it."
    )


UNCOSTED_ATTEMPT_REASON = (
    "An attempt of this job has no resource evidence, so its cost is missing from this amount."
)


def _attempt_in_period(attempt: ExecutionAttempt, query: ReportQuery, completed_mode: bool) -> bool:
    """Whether an attempt's own timing, when it has any, falls inside the report period.

    Untimed attempts cannot be placed, so they are treated as inside: excluding
    them would let a missing cost disappear from every period at once.
    """
    if completed_mode or attempt.tool_started_at is None:
        return True
    end = attempt.tool_finished_at or attempt.tool_started_at
    return _slice_fraction(attempt.tool_started_at, max(end, attempt.tool_started_at), query) > 0


def _executions(
    attempts: list[ExecutionAttempt], with_resources: set[uuid.UUID]
) -> tuple[list[ExecutionAttempt], list[ExecutionAttempt], str]:
    """Reconcile observations into logical execution attempts.

    Returns the first attempt of each task, its repeats, and which evidence the
    reconciliation rests on. Galaxy's record of a job describes one of the
    provider-observed executions whenever those exist, unless it carries a
    resource of its own. Parallel tasks of one submission are not repeats; a
    later ordinal of the same task, or a later submission of it, is.
    """
    provider = [a for a in attempts if a.source_attempt_id != GALAXY_RECORD_ATTEMPT_ID]
    galaxy = [a for a in attempts if a.source_attempt_id == GALAXY_RECORD_ATTEMPT_ID]
    if provider:
        logical = provider + [a for a in galaxy if a.id in with_resources]
        evidence = "provider"
    else:
        logical = galaxy
        evidence = "galaxy_record" if galaxy else "none"
    tasks: dict[int | None, list[ExecutionAttempt]] = defaultdict(list)
    for attempt in logical:
        tasks[attempt.task_index].append(attempt)
    first: list[ExecutionAttempt] = []
    repeats: list[ExecutionAttempt] = []
    for group in tasks.values():
        group.sort(key=lambda a: (
            a.tool_started_at is None, a.tool_started_at or datetime.min.replace(tzinfo=UTC),
            a.attempt_ordinal or 0, a.source_attempt_id,
        ))
        first.append(group[0])
        repeats.extend(group[1:])
    return first, repeats, evidence


def _line_slices(
    line: CostLine, lifetime: ResourceLifetime, as_of: datetime
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
    if lifetime.observed_start:
        end = lifetime.observed_end or as_of
        if end >= lifetime.observed_start:
            return [(lifetime.observed_start, end, line.amount)]
    return []


def _slice_fraction(start: datetime, end: datetime, query: ReportQuery) -> Decimal:
    if start == end:
        # An instant has no duration to apportion; it belongs to the half-open
        # interval that contains it, whole.
        inside = (not query.from_time or start >= query.from_time) and (
            not query.to_time or start < query.to_time
        )
        return Decimal("1") if inside else ZERO
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
    undated: list[dict] | None = None,
) -> tuple[list[dict], CostRevision | None]:
    """Report records for the query, one per authorized job.

    Under a date filter, a job whose cost cannot be placed in time belongs to no
    period: it is left out of the result and appended to `undated`, so period
    totals, counts and rankings never absorb it.
    """
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
    line_map: dict[uuid.UUID, list[tuple[CostLine, ResourceLifetime]]] = defaultdict(list)
    attempt_map: dict[uuid.UUID, list[ExecutionAttempt]] = defaultdict(list)
    lifetime_attempts: dict[uuid.UUID, list[ExecutionAttempt]] = defaultdict(list)
    if job_ids:
        for attempt in session.scalars(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.job_id.in_(job_ids))
            .order_by(ExecutionAttempt.source_attempt_id)
        ):
            attempt_map[attempt.job_id].append(attempt)
        for lifetime_id, attempt in session.execute(
            select(LifetimeAttempt.lifetime_id, ExecutionAttempt)
            .join(ExecutionAttempt, LifetimeAttempt.attempt_id == ExecutionAttempt.id)
            .where(ExecutionAttempt.job_id.in_(job_ids))
        ):
            lifetime_attempts[lifetime_id].append(attempt)
    if revision and job_ids:
        rows = session.execute(
            select(CostLine, ResourceLifetime)
            .join(ResourceLifetime, CostLine.lifetime_id == ResourceLifetime.id)
            .where(
                CostLine.revision_id == revision.id,
                CostLine.basis == query.basis,
                CostLine.job_id.in_(job_ids),
            )
        )
        for line, lifetime in rows:
            line_map[line.job_id].append((line, lifetime))

    invocation_jobs: set[uuid.UUID] | None = None
    if query.invocation_id or query.workflow_id:
        invocation_jobs = workflow_jobs

    resourced_attempts = {
        attempt.id for users in lifetime_attempts.values() for attempt in users
    }
    as_of = revision.created_at if revision else datetime.now(UTC)
    dated = bool(query.from_time or query.to_time)
    records: list[dict] = []
    for job, owner in jobs:
        if invocation_jobs is not None and job.id not in invocation_jobs:
            continue
        pairs = line_map[job.id]
        attempts = attempt_map[job.id]
        lines = [pair[0] for pair in pairs]
        quality = _quality(lines, job.state)
        first, repeats, attempt_evidence = _executions(attempts, resourced_attempts)
        repeat_ids = {attempt.id for attempt in repeats}
        completions = [
            attempt.tool_finished_at for attempt in attempts
            if attempt.outcome == "ok" and attempt.tool_finished_at
        ]
        completed_at = max(completions) if completions else None
        completed_mode = query.mode == "completed"
        if completed_mode and (
            not completed_at or (query.from_time and completed_at < query.from_time)
            or (query.to_time and completed_at >= query.to_time)
        ):
            continue
        known_amounts: list[Decimal] = []
        unattributed = ZERO
        capacities: set[str] = set()
        interval_hit = False
        has_timing = False
        repeat_amount = ZERO
        repeat_shared_amount = ZERO
        repeat_incomplete = False
        for line, lifetime in pairs:
            capacities.add(lifetime.capacity_relationship.value)
            slices = _line_slices(line, lifetime, as_of)
            has_timing = has_timing or bool(slices)
            if not slices and line.amount is not None:
                unattributed += line.amount
            line_hit = False
            line_amount: Decimal | None = None
            for start, end, slice_amount in slices:
                fraction = _slice_fraction(start, end, query)
                if fraction > 0:
                    line_hit = True
                    if slice_amount is not None:
                        line_amount = (line_amount or ZERO) + slice_amount * min(fraction, Decimal("1"))
            if completed_mode:
                line_hit, line_amount = True, line.amount
            interval_hit = interval_hit or line_hit
            if line_amount is not None:
                known_amounts.append(line_amount)
            users = {
                attempt.id for attempt in lifetime_attempts.get(lifetime.id, [])
                if attempt.job_id == job.id
            }
            if not line_hit or not users & repeat_ids:
                continue
            if line_amount is None:
                repeat_incomplete = True
            elif users <= repeat_ids:
                repeat_amount += line_amount
            else:
                # One resource served the first attempt and a repeat; its
                # charge cannot be divided between them without a policy.
                repeat_shared_amount += line_amount
                repeat_incomplete = True
        if dated and not completed_mode and has_timing and not interval_hit:
            continue
        # An execution with no resource evidence has a cost this job's lines
        # cannot include, however complete the lines themselves are.
        costed = {
            attempt.id for _, lifetime in pairs
            for attempt in lifetime_attempts.get(lifetime.id, []) if attempt.job_id == job.id
        }
        uncosted = [
            attempt for attempt in [*first, *repeats]
            if attempt.id not in costed and _attempt_in_period(attempt, query, completed_mode)
        ]
        if lines and uncosted and quality != Quality.unpriced.value:
            quality = Quality.partial.value
        if any(attempt.id in repeat_ids for attempt in uncosted):
            repeat_incomplete = True
        amount = sum(known_amounts, ZERO) if known_amounts else None
        if any(line.amount is not None for line in lines) and amount is None and not dated:
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
        record = {
            "id": str(job.id), "source_id": job.source_id, "tool_id": job.tool_id,
            "tool_name": tool_display_name(job.tool_id),
            "tool_version": job.tool_version, "owner": owner.label, "owner_id": owner.source_id,
            "state": job.state, "runner": job.runner, "destination": job.destination,
            "created_at": job.created_at, "updated_at": job.updated_at, "amount": amount,
            "currency": "USD", "quality": quality,
            "reason": " | ".join(sorted(
                {line.reason for line in lines} | ({UNCOSTED_ATTEMPT_REASON} if lines and uncosted else set())
            )) or _missing_evidence_reason(job, attempts),
            "cost_lines": len(lines),
            "attempt_count": len(first) + len(repeats),
            "repeat_attempt_count": len(repeats),
            "attempt_evidence": attempt_evidence,
            "observation_count": len(attempts),
            "repeat_amount": repeat_amount,
            "repeat_shared_amount": repeat_shared_amount,
            "repeat_amount_complete": not repeat_incomplete,
            "capacities": sorted(capacities),
            "unattributed_amount": unattributed,
            "temporally_unattributed": not has_timing,
            "completed_at": completed_at,
            "full_amount": sum((line.amount for line in lines if line.amount is not None), ZERO)
            if any(line.amount is not None for line in lines) else None,
            "job": job, "pairs": pairs, "attempts": attempts,
            "lifetime_attempts": lifetime_attempts,
        }
        if dated and not completed_mode and not has_timing:
            if undated is not None:
                undated.append(record)
            continue
        records.append(record)
    return records, revision


def _window(query: ReportQuery, records: list[dict]) -> dict:
    starts = [
        lifetime.observed_start
        for record in records for _, lifetime in record["pairs"]
        if lifetime.observed_start
    ]
    ends = [
        lifetime.observed_end
        for record in records for _, lifetime in record["pairs"]
        if lifetime.observed_end
    ]
    lower = query.from_time or (min(starts) if starts else None)
    upper = query.to_time or (max(ends) if ends else None)
    return {
        "from": lower.isoformat() if lower else None, "to": upper.isoformat() if upper else None,
        "timezone": query.timezone, "semantics": "[from, to)", "mode": query.mode,
    }


def _undated_meta(query: ReportQuery, undated: list[dict] | None) -> dict | None:
    """Evidence no period can hold, reported beside a dated result, never in it."""
    if not (query.from_time or query.to_time) or query.mode == "completed" or undated is None:
        return None
    known = [r["full_amount"] for r in undated if r["full_amount"] is not None]
    return {
        "job_count": len(undated),
        "amount": str(sum(known, ZERO)) if known else None,
        "incomplete": sum(r["quality"] in INCOMPLETE_QUALITIES for r in undated),
    }


def _meta(
    session: Session, identity: Identity, query: ReportQuery, revision, records: list[dict],
    undated: list[dict] | None = None,
) -> dict:
    priced = [r for r in records if r["amount"] is not None]
    return {
        "basis": query.basis, "currency": "USD",
        "applied_filters": query.model_dump(
            mode="json", exclude={"limit", "offset", "undated_offset", "sort", "direction"}
        ),
        "observation_window": _window(query, records),
        "revision_id": str(revision.id) if revision else None,
        "calculation_version": revision.calculation_version if revision else None,
        "as_of": revision.created_at.isoformat() if revision else None,
        "priced_subtotal": str(sum((r["amount"] for r in priced), ZERO)) if priced else None,
        "coverage": {
            "jobs": len(records), "priced": len(priced),
            "incomplete": sum(r["quality"] in INCOMPLETE_QUALITIES for r in records),
            "known_zero": sum(r["quality"] == "known_zero" for r in records),
            "temporally_unattributed": sum(r["temporally_unattributed"] for r in records),
        },
        "undated": _undated_meta(query, undated),
    }


def _public(record: dict) -> dict:
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in record.items()
        if key not in {
            "job", "pairs", "attempts", "lifetime_attempts", "full_amount",
            "repeat_amount", "repeat_shared_amount", "repeat_amount_complete",
        }
    }


def _repeated_work(records: list[dict]) -> dict:
    """Failed and repeated work, each already part of the period total.

    "Jobs that repeated" is the whole cost of any job with a repeat attempt;
    "repeat attempts" is only what the repeats themselves used. A resource
    shared by a first attempt and its repeat belongs to neither alone, so it is
    reported separately and leaves the attributable subtotal incomplete.
    """
    failed = [r for r in records if r["state"] in {"error", "failed"}]
    repeated = [r for r in records if r["repeat_attempt_count"]]
    return {
        "failed_spend": str(sum((r["amount"] or ZERO for r in failed), ZERO)),
        "failed_job_count": len(failed),
        "failed_incomplete_job_count": sum(r["quality"] in INCOMPLETE_QUALITIES for r in failed),
        "repeated_job_spend": str(sum((r["amount"] or ZERO for r in repeated), ZERO)),
        "repeated_job_count": len(repeated),
        "repeat_attempt_spend": str(sum((r["repeat_amount"] for r in repeated), ZERO)),
        "repeat_attempt_shared_spend": str(sum((r["repeat_shared_amount"] for r in repeated), ZERO)),
        "repeat_attempt_spend_complete": all(r["repeat_amount_complete"] for r in repeated),
    }


def _imported_snapshot(capabilities: dict) -> dict | None:
    """Real facts restored from a capture, which are neither live nor synthetic.

    The loader that restores a capture records its manifest here; only the
    fields a report needs to describe the capture are passed on.
    """
    manifest = capabilities.get("imported_snapshot")
    if not isinstance(manifest, dict):
        return None
    return {
        key: manifest.get(key)
        for key in ("captured_at", "source_cutoffs", "snapshot_digest", "label")
    }


def summary(session: Session, identity: Identity, query: ReportQuery) -> dict:
    undated: list[dict] = []
    records, revision = _base_records(session, identity, query, undated=undated)
    meta = _meta(session, identity, query, revision, records, undated)
    tenant = session.get(Tenant, identity.tenant_id)
    capabilities = tenant.capabilities or {}
    demo = bool(capabilities.get("demo", True))
    imported = _imported_snapshot(capabilities)
    demo_period = None
    if demo or imported:
        # Fixture and imported data have a fixed date range; saying when it is
        # beats leaving a first-time user with an empty period and no
        # explanation.
        window = session.execute(
            select(
                func.min(ResourceLifetime.observed_start),
                func.max(ResourceLifetime.observed_end),
            ).where(ResourceLifetime.tenant_id == identity.tenant_id)
        ).first()
        if window and window[0] and window[1]:
            demo_period = {"from": window[0].isoformat(), "to": window[1].isoformat()}
    infra = infrastructure(
        session, identity, query, revision=revision, snapshot_validated=True
    ) if identity.can_view_infrastructure else None
    return {
        **meta, "amount": meta["priced_subtotal"], "job_count": len(records),
        "priced_job_count": meta["coverage"]["priced"],
        "unpriced_job_count": meta["coverage"]["incomplete"],
        "known_zero_job_count": meta["coverage"]["known_zero"],
        **_repeated_work(records),
        "baseline_infrastructure_amount": infra["amount"] if infra else None,
        "baseline_infrastructure_observed": infra["observed_coverage"] if infra else None,
        "can_view_infrastructure": identity.can_view_infrastructure,
        "demo": demo,
        "demo_period": demo_period,
        "imported_snapshot": imported,
    }


def _sorted(records: list[dict], query: ReportQuery) -> list[dict]:
    known = [r for r in records if r[query.sort] is not None]
    missing = [r for r in records if r[query.sort] is None]
    known.sort(key=lambda r: (r[query.sort], r["id"]), reverse=query.direction == "desc")
    return known + sorted(missing, key=lambda r: r["id"])


def list_jobs(session: Session, identity: Identity, query: ReportQuery, paginate: bool = True) -> dict:
    if query.sort not in SORT_FIELDS:
        raise HTTPException(422, f"Unsupported sort field: {query.sort}")
    undated: list[dict] = []
    records, revision = _base_records(session, identity, query, undated=undated)
    records = _sorted(records, query)
    page = records[query.offset:query.offset + query.limit] if paginate else records
    return {
        "items": [_public(r) for r in page], "total": len(records),
        "limit": query.limit, "offset": query.offset,
        # Outside every period's totals, but still inspectable beside them.
        "undated_items": [
            _public(r) for r in
            _sorted(undated, query)[query.undated_offset:query.undated_offset + query.limit]
        ],
        "undated_offset": query.undated_offset,
        "meta": _meta(session, identity, query, revision, records, undated),
    }


def job_detail(session: Session, identity: Identity, job_id: uuid.UUID, query: ReportQuery) -> dict | None:
    unrestricted = query.model_copy(update={
        "search": None, "tool_id": None, "tool_version": None, "invocation_id": None,
        "min_cost": None, "max_cost": None, "quality": None,
    })
    undated: list[dict] = []
    records, revision = _base_records(session, identity, unrestricted, undated=undated)
    record = next((r for r in [*records, *undated] if r["id"] == str(job_id)), None)
    if not record:
        return None
    full_records, _ = _base_records(session, identity, unrestricted.model_copy(update={"from_time": None, "to_time": None}))
    full = next(r for r in full_records if r["id"] == str(job_id))
    lifetime_attempts = record["lifetime_attempts"]
    resources = []
    charged_alone: dict[uuid.UUID, dict] = {}
    for line, lifetime in record["pairs"]:
        sharing = sorted(
            lifetime_attempts.get(lifetime.id, []), key=lambda value: value.source_attempt_id
        )
        entry = {
            "lifetime_id": str(lifetime.id),
            "resource_key": lifetime.resource_key,
            "resource_uid": lifetime.resource_uid,
            "provider": lifetime.provider,
            "machine_type": lifetime.machine_type,
            "region": lifetime.region,
            "zone": lifetime.zone,
            "purchase_model": lifetime.purchase_model,
            "capacity_relationship": lifetime.capacity_relationship.value,
            "resource_started_at": lifetime.observed_start,
            "resource_finished_at": lifetime.observed_end,
            "timing_method": lifetime.timing_method,
            "requested_vcpu": str(lifetime.requested_vcpu) if lifetime.requested_vcpu is not None else None,
            "requested_memory_mib": (
                str(lifetime.requested_memory_mib)
                if lifetime.requested_memory_mib is not None
                else None
            ),
            "provisioned": lifetime.facts,
            "amount": str(line.amount) if line.amount is not None else None,
            "quality": _quality([line]),
            "reason": line.reason,
            "provenance": line.details,
            "shared_attempt_ids": [str(attempt.id) for attempt in sharing],
            "shared_attempt_count": len(sharing),
        }
        resources.append(entry)
        if len(sharing) == 1:
            charged_alone[sharing[0].id] = entry
    first, repeats, _ = _executions(record["attempts"], {
        attempt.id for users in lifetime_attempts.values() for attempt in users
    })
    roles = {attempt.id: "first" for attempt in first} | {attempt.id: "repeat" for attempt in repeats}
    attempts = []
    for attempt in record["attempts"]:
        used = [
            entry for entry in resources
            if str(attempt.id) in entry["shared_attempt_ids"]
        ]
        alone = charged_alone.get(attempt.id)
        attempts.append({
            "id": str(attempt.id), "source_attempt_id": attempt.source_attempt_id,
            "runner": attempt.runner, "outcome": attempt.outcome,
            "provider_outcome": attempt.provider_outcome, "exit_code": attempt.exit_code,
            "task_index": attempt.task_index, "attempt_ordinal": attempt.attempt_ordinal,
            "tool_started_at": attempt.tool_started_at,
            "tool_finished_at": attempt.tool_finished_at,
            # An observation describes an execution counted under another row.
            "role": roles.get(attempt.id, "observation"),
            "resource_keys": [entry["resource_key"] for entry in used],
            # A lifetime shared by retries is charged once; its amount appears
            # on the resource, not repeated on each attempt row.
            "amount": alone["amount"] if alone else None,
            "amount_shared_with_attempts": [
                other for entry in used for other in entry["shared_attempt_ids"]
                if len(entry["shared_attempt_ids"]) > 1 and other != str(attempt.id)
            ],
        })
    result = _public(record)
    result.update({
        "interval_amount": result["amount"],
        "full_job_amount": str(full["amount"]) if full["amount"] is not None else None,
        "basis": query.basis, "attempts": attempts, "resources": resources,
        "revision_id": str(revision.id) if revision else None,
    })
    result["cost"] = {
        "basis": query.basis, "amount": result["interval_amount"], "currency": "USD",
        "quality": result["quality"], "reason": result["reason"],
        "attempts": attempts, "resources": resources,
    }
    return result


def tools(session: Session, identity: Identity, query: ReportQuery) -> dict:
    undated: list[dict] = []
    records, revision = _base_records(session, identity, query, undated=undated)
    groups: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    for record in records:
        groups[(record["tool_id"], record["tool_version"])].append(record)
    items = []
    for (tool_id, version), rows in groups.items():
        known = [r["amount"] for r in rows if r["amount"] is not None]
        complete_rows = [
            r for r in rows
            if r["state"] == "ok" and r["full_amount"] is not None
            and r["quality"] not in INCOMPLETE_QUALITIES
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
            "tool_id": tool_id, "tool_name": tool_display_name(tool_id),
            "tool_version": version, "job_count": len(rows),
            "amount": str(sum(known, ZERO)) if known else None, "priced_count": len(known),
            "incomplete_count": sum(r["quality"] in INCOMPLETE_QUALITIES for r in rows),
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
        "meta": _meta(session, identity, query, revision, records, undated),
    }


def _run_status(invocation_state: str, rows: list[dict]) -> str:
    """A run's status, derived from its executions rather than its scheduling.

    A scheduled invocation has only finished *scheduling*; its tool executions
    may still be running or may have failed.
    """
    states = {row["state"] for row in rows}
    if invocation_state in {"cancelled", "cancelling"}:
        return "cancelled"
    if states & {"error", "failed"}:
        return "failed"
    if invocation_state == "failed":
        return "failed"
    if states & RUNNING_STATES or invocation_state not in {"scheduled", "completed", "ok"}:
        return "running"
    if not rows:
        return "no runs recorded"
    return "completed"


def invocations(session: Session, identity: Identity, query: ReportQuery, roots_only: bool = True) -> dict:
    """Workflow runs, with a full-run total beside the selected period's cost.

    "What did this run cost?" and "what did I spend last week?" are different
    questions: the run total covers the whole run, while the period amount is
    the part accrued inside the selected interval.
    """
    undated: list[dict] = []
    records, revision = _base_records(session, identity, query, undated=undated)
    by_id = {uuid.UUID(r["id"]): r for r in records}
    full_query = query.model_copy(update={"from_time": None, "to_time": None})
    dated = bool(query.from_time or query.to_time)
    full_records = (
        _base_records(session, identity, full_query)[0] if dated else records
    )
    full_by_id = {uuid.UUID(r["id"]): r for r in full_records}
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
        full_rows = [full_by_id[job_id] for job_id in job_ids if job_id in full_by_id]
        search_hit = (query.search or "").casefold() in (
            f"{inv.source_id} {inv.workflow_name} {inv.workflow_version or ''}".casefold()
        )
        if query.search and not search_hit and not rows:
            continue
        started_at = min(
            (row["created_at"] for row in full_rows), default=inv.created_at
        )
        # The period scopes which runs are listed: a run appears when it
        # accrued cost inside it, or when the run itself started inside it.
        if dated and not rows:
            within = (not query.from_time or started_at >= query.from_time) and (
                not query.to_time or started_at < query.to_time
            )
            if not within:
                continue
        known = [r["amount"] for r in rows if r["amount"] is not None]
        full_known = [r["full_amount"] for r in full_rows if r["full_amount"] is not None]
        incomplete = sum(
            r["quality"] in INCOMPLETE_QUALITIES for r in full_rows
        )
        items.append({
            "id": str(inv.id), "source_id": inv.source_id,
            "workflow_id": inv.workflow_id or inv.source_id,
            "workflow_name": inv.workflow_name, "workflow_version": inv.workflow_version,
            "parent_id": str(inv.parent_id) if inv.parent_id else None, "state": inv.state,
            "run_status": _run_status(inv.state, full_rows),
            "started_at": started_at,
            "job_count": len(rows),
            "run_job_count": len(full_rows),
            # The period's share of this run, and the run as a whole.
            "amount": str(sum(known, ZERO)) if known else None,
            "run_total": str(sum(full_known, ZERO)) if full_known else None,
            "run_total_complete": incomplete == 0 and bool(full_rows),
            "currency": "USD",
            "unpriced_job_count": sum(r["quality"] in INCOMPLETE_QUALITIES for r in rows),
            "run_unpriced_job_count": incomplete,
            "reused_job_count": sum(bool(r["job"].copied_from_source_id) for r in full_rows),
            "timing_unavailable": bool(full_rows) and all(
                row["temporally_unattributed"] for row in full_rows
            ),
        })
    return {
        "items": items[query.offset:query.offset + query.limit], "total": len(items),
        "limit": query.limit, "offset": query.offset,
        "meta": _meta(session, identity, query, revision, records, undated),
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
    listed = list_jobs(
        session, identity,
        query.model_copy(update={"invocation_id": str(invocation_id), "limit": 200}),
        paginate=False,
    )
    by_id = {job["id"]: job for job in [*listed["items"], *listed["undated_items"]]}
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
    undated: list[dict] = []
    records, revision = _base_records(session, identity, query, undated=undated)
    timezone = ZoneInfo(query.timezone)
    buckets: dict[str, dict] = defaultdict(lambda: {
        "amount": ZERO, "job_ids": set(), "incomplete_ids": set(), "provisional": False,
        "by_runner": defaultdict(Decimal), "by_owner": defaultdict(Decimal),
        "by_tool": defaultdict(Decimal),
    })
    as_of = revision.created_at if revision else datetime.now(UTC)

    def accrue(record: dict, instant: datetime, amount: Decimal | None, open_ended: bool) -> None:
        bucket = buckets[instant.astimezone(timezone).date().isoformat()]
        bucket["job_ids"].add(record["id"])
        bucket["provisional"] = bucket["provisional"] or open_ended
        if amount is None:
            bucket["incomplete_ids"].add(record["id"])
            return
        bucket["amount"] += amount
        bucket["by_runner"][record["runner"] or "unknown"] += amount
        bucket["by_owner"][record["owner"]] += amount
        bucket["by_tool"][f"{record['tool_id']}@{record['tool_version'] or ''}"] += amount

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
        for line, lifetime in record["pairs"]:
            open_ended = lifetime.observed_end is None
            for start, end, slice_amount in _line_slices(line, lifetime, as_of):
                if start == end:
                    if _slice_fraction(start, end, query) > 0:
                        accrue(record, start, slice_amount, open_ended)
                    continue
                total_seconds = Decimal(str((end - start).total_seconds()))
                cursor = max(start, query.from_time or start)
                limit = min(end, query.to_time or end)
                while cursor < limit:
                    local = cursor.astimezone(timezone)
                    next_day = datetime.combine(
                        local.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone
                    ).astimezone(UTC)
                    chunk_end = min(limit, next_day)
                    accrue(record, cursor, None if slice_amount is None else (
                        slice_amount * Decimal(str((chunk_end - cursor).total_seconds())) / total_seconds
                    ), open_ended)
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
        "temporally_unattributed_count": sum(
            r["temporally_unattributed"] for r in [*records, *undated]
        ),
        "temporally_unattributed_subtotal": str(
            sum((r["unattributed_amount"] for r in [*records, *undated]), ZERO)
        ),
        "meta": _meta(session, identity, query, revision, records, undated),
    }


def users(session: Session, identity: Identity, query: ReportQuery) -> dict:
    if not identity.is_admin:
        raise HTTPException(403, "Administrator scope required")
    undated: list[dict] = []
    records, revision = _base_records(session, identity, query, undated=undated)
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
        "meta": _meta(session, identity, query, revision, records, undated),
    }


def infrastructure(
    session: Session,
    identity: Identity,
    query: ReportQuery,
    revision: CostRevision | None = None,
    snapshot_validated: bool = False,
) -> dict:
    if not identity.can_view_infrastructure:
        raise HTTPException(403, "Infrastructure reporting is not authorized for this scope")
    if not snapshot_validated:
        revision = _revision(session, identity, query.revision)
    rows = session.scalars(
        select(InfrastructureInterval)
        .where(InfrastructureInterval.tenant_id == identity.tenant_id)
        .order_by(InfrastructureInterval.observed_start)
    ).all()
    items = []
    covered: list[tuple[datetime, datetime]] = []
    for row in rows:
        start, end = row.observed_start, row.observed_end
        overlap_start, overlap_end = max(start, query.from_time or start), min(end, query.to_time or end)
        fraction = max(0.0, (overlap_end - overlap_start).total_seconds()) / (end - start).total_seconds()
        if fraction > 0:
            covered.append((overlap_start, overlap_end))
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
        "revision_id": str(revision.id) if revision else None,
        "as_of": revision.created_at.isoformat() if revision else None,
        "observation_window": {
            "from": (query.from_time or (min((row.observed_start for row in rows), default=None))).isoformat()
            if (query.from_time or rows) else None,
            "to": (query.to_time or (max((row.observed_end for row in rows), default=None))).isoformat()
            if (query.to_time or rows) else None,
            "timezone": query.timezone, "semantics": "[from, to)", "mode": query.mode,
        },
        # What the server was actually observed doing inside the requested
        # window; the window itself is only what was asked for.
        "observed_coverage": {
            "from": min(start for start, _ in covered).isoformat(),
            "to": max(end for _, end in covered).isoformat(),
        } if covered else None,
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
    gaps = session.scalars(
        select(ObservationGap)
        .where(ObservationGap.tenant_id == identity.tenant_id)
        .order_by(ObservationGap.detected_at.desc())
        .limit(20)
    ).all()
    statuses = {source["status"] for source in sources}
    overall = "failed" if "failed" in statuses else (
        "partial" if statuses - {"healthy", "historical_snapshot"} else "healthy"
    )
    return {
        "sources": sources,
        "overall_status": overall,
        "observation_gaps": [
            {
                "source": gap.source, "kind": gap.kind, "detected_at": gap.detected_at,
                "gap_start": gap.gap_start, "gap_end": gap.gap_end,
                "recoverable": gap.recoverable, "detail": gap.detail,
            }
            for gap in gaps
        ],
    }


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
