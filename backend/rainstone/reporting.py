import uuid
from types import SimpleNamespace

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from rainstone.auth import Identity
from rainstone.models import (
    CostLine,
    CostRevision,
    InfrastructureInterval,
    IngestionState,
    Invocation,
    InvocationJob,
    Job,
    Owner,
    PriceVersion,
    Quality,
)


def latest_revision_id(session: Session, tenant_id: uuid.UUID) -> uuid.UUID | None:
    return session.scalar(
        select(CostRevision.id)
        .where(CostRevision.tenant_id == tenant_id)
        .order_by(CostRevision.created_at.desc(), CostRevision.id.desc())
        .limit(1)
    )


def authorized_jobs(statement: Select, identity: Identity) -> Select:
    statement = statement.where(Job.tenant_id == identity.tenant_id)
    return statement if identity.is_admin else statement.where(Job.owner_id == identity.owner_id)


def job_costs(revision_id: uuid.UUID | None, basis: str):
    return (
        select(
            CostLine.job_id.label("job_id"),
            func.coalesce(func.sum(CostLine.amount), 0).label("amount"),
            func.count().filter(CostLine.amount.is_not(None)).label("priced_lines"),
            func.count().filter(CostLine.amount.is_(None)).label("missing_lines"),
            func.count().filter(CostLine.quality == Quality.approximate).label("approximate_lines"),
            func.count().filter(CostLine.quality == Quality.partial).label("partial_lines"),
            func.count().filter(CostLine.quality == Quality.unpriced).label("unpriced_lines"),
            func.count().filter(CostLine.quality == Quality.known_zero).label("known_zero_lines"),
            func.count().label("line_count"),
            func.string_agg(CostLine.reason.distinct(), " | ").label("reason"),
        )
        .where(CostLine.revision_id == revision_id, CostLine.basis == basis)
        .group_by(CostLine.job_id)
        .subquery()
    )


def cost_quality(row) -> str:
    if row.partial_lines:
        return Quality.partial.value
    if row.unpriced_lines:
        return Quality.unpriced.value
    if row.missing_lines:
        return Quality.partial.value
    if row.approximate_lines:
        return Quality.approximate.value
    if row.known_zero_lines == row.line_count:
        return Quality.known_zero.value
    return Quality.complete.value


def summary(session: Session, identity: Identity, basis: str) -> dict:
    revision_id = latest_revision_id(session, identity.tenant_id)
    costs = job_costs(revision_id, basis)
    statement = select(
        func.count(Job.id),
        func.coalesce(func.sum(costs.c.amount), 0),
        func.count(Job.id).filter(costs.c.priced_lines > 0),
        func.count(Job.id).filter(costs.c.missing_lines > 0),
        func.count(Job.id).filter(costs.c.known_zero_lines == costs.c.line_count),
    ).join(costs, costs.c.job_id == Job.id)
    row = session.execute(authorized_jobs(statement, identity)).one()
    infra = session.scalar(
        select(func.coalesce(func.sum(InfrastructureInterval.amount), 0)).where(
            InfrastructureInterval.tenant_id == identity.tenant_id
        )
    )
    return {
        "basis": basis,
        "currency": "USD",
        "amount": str(row[1]),
        "job_count": row[0],
        "priced_job_count": row[2],
        "unpriced_job_count": row[3],
        "known_zero_job_count": row[4],
        "baseline_infrastructure_amount": str(infra),
        "revision_id": str(revision_id) if revision_id else None,
    }


def list_jobs(
    session: Session,
    identity: Identity,
    basis: str,
    limit: int,
    offset: int,
    state: str | None,
    runner: str | None,
    search: str | None,
) -> dict:
    revision_id = latest_revision_id(session, identity.tenant_id)
    costs = job_costs(revision_id, basis)
    statement = (
        select(Job, Owner, costs).join(Owner, Job.owner_id == Owner.id).join(costs, costs.c.job_id == Job.id)
    )
    statement = authorized_jobs(statement, identity)
    if state:
        statement = statement.where(Job.state == state)
    if runner:
        statement = statement.where(Job.runner == runner)
    if search:
        term = f"%{search}%"
        statement = statement.where(Job.tool_id.ilike(term) | Job.source_id.ilike(term))
    total = session.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    rows = session.execute(
        statement.order_by(Job.created_at.desc(), Job.source_id).limit(limit).offset(offset)
    ).all()
    items = []
    for row in rows:
        job, owner = row[0], row[1]
        line = SimpleNamespace(**{key: row._mapping[key] for key in costs.c.keys()})
        items.append(
            {
                "id": str(job.id),
                "source_id": job.source_id,
                "tool_id": job.tool_id,
                "tool_version": job.tool_version,
                "owner": owner.label,
                "state": job.state,
                "runner": job.runner,
                "created_at": job.created_at,
                "amount": str(line.amount) if line.priced_lines else None,
                "currency": "USD",
                "quality": cost_quality(line),
                "reason": line.reason,
                "attempt_cost_lines": line.line_count,
            }
        )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def job_detail(session: Session, identity: Identity, job_id: uuid.UUID, basis: str) -> dict | None:
    revision_id = latest_revision_id(session, identity.tenant_id)
    costs = job_costs(revision_id, basis)
    statement = (
        select(Job, Owner, costs)
        .join(Owner, Job.owner_id == Owner.id)
        .join(costs, costs.c.job_id == Job.id)
        .where(Job.id == job_id)
    )
    row = session.execute(authorized_jobs(statement, identity)).first()
    if not row:
        return None
    job, owner = row[0], row[1]
    line = SimpleNamespace(**{key: row._mapping[key] for key in costs.c.keys()})
    attempts = session.execute(
        select(CostLine)
        .where(
            CostLine.job_id == job.id,
            CostLine.revision_id == revision_id,
            CostLine.basis == basis,
        )
        .order_by(CostLine.attempt_id)
    ).scalars()
    return {
        "id": str(job.id),
        "source_id": job.source_id,
        "tool_id": job.tool_id,
        "tool_version": job.tool_version,
        "owner": owner.label,
        "state": job.state,
        "runner": job.runner,
        "destination": job.destination,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "cost": {
            "basis": basis,
            "amount": str(line.amount) if line.priced_lines else None,
            "currency": "USD",
            "quality": cost_quality(line),
            "reason": line.reason,
            "attempts": [
                {
                    "attempt_id": str(item.attempt_id),
                    "amount": str(item.amount) if item.amount is not None else None,
                    "quality": item.quality.value,
                    "reason": item.reason,
                    "details": item.details,
                }
                for item in attempts
            ],
        },
    }


def list_invocations(session: Session, identity: Identity, basis: str) -> list[dict]:
    revision_id = latest_revision_id(session, identity.tenant_id)
    costs = job_costs(revision_id, basis)
    memberships = select(InvocationJob.invocation_id, InvocationJob.job_id).distinct().subquery()
    statement = (
        select(
            Invocation,
            func.count(func.distinct(Job.id)).label("job_count"),
            func.coalesce(func.sum(costs.c.amount), 0).label("amount"),
            func.count(Job.id).filter(costs.c.missing_lines > 0).label("unpriced"),
        )
        .join(memberships, memberships.c.invocation_id == Invocation.id)
        .join(Job, Job.id == memberships.c.job_id)
        .join(costs, costs.c.job_id == Job.id)
        .where(
            Invocation.tenant_id == identity.tenant_id,
        )
        .group_by(Invocation.id)
        .order_by(Invocation.created_at)
    )
    if not identity.is_admin:
        statement = statement.where(Invocation.owner_id == identity.owner_id)
    return [
        {
            "id": str(inv.id),
            "source_id": inv.source_id,
            "workflow_name": inv.workflow_name,
            "parent_id": str(inv.parent_id) if inv.parent_id else None,
            "job_count": count,
            "amount": str(amount),
            "currency": "USD",
            "unpriced_job_count": unpriced,
        }
        for inv, count, amount, unpriced in session.execute(statement)
    ]


def freshness(session: Session, identity: Identity) -> list[dict]:
    rows = session.scalars(
        select(IngestionState)
        .where(IngestionState.tenant_id == identity.tenant_id)
        .order_by(IngestionState.source)
    ).all()
    result = [
        {
            "source": row.source,
            "status": row.status,
            "last_success_at": row.last_success_at,
            "cursor": row.cursor,
        }
        for row in rows
    ]
    latest_price = session.execute(
        select(PriceVersion.catalog_id, PriceVersion.observed_at)
        .order_by(PriceVersion.observed_at.desc())
        .limit(1)
    ).first()
    if latest_price:
        result.append(
            {
                "source": "price_catalog",
                "status": "historical_snapshot",
                "last_success_at": latest_price.observed_at,
                "cursor": {"catalog_id": latest_price.catalog_id},
            }
        )
    return result
