import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from rainstone.models import (
    CapacityRelationship,
    CostLine,
    CostRevision,
    DeploymentPolicy,
    ExecutionAttempt,
    InfrastructureInterval,
    Invocation,
    InvocationJob,
    Job,
    Owner,
    PriceVersion,
    Quality,
    ResourceInterval,
    Tenant,
)

CALCULATION_VERSION = "phase2a-v2"


@dataclass(frozen=True)
class CalculatedLine:
    basis: str
    amount: Decimal | None
    quality: Quality
    reason: str
    billed_seconds: Decimal | None = None
    allocations: tuple[dict, ...] = ()


def calculate_interval(
    interval: ResourceInterval, price: PriceVersion | list[PriceVersion] | None
) -> list[CalculatedLine]:
    relationship = interval.capacity_relationship
    if relationship == CapacityRelationship.existing:
        return [
            CalculatedLine(
                "additional",
                Decimal("0"),
                Quality.known_zero,
                "Uses declared baseline capacity under the unchanged size and uptime assumption.",
            ),
            CalculatedLine(
                "allocated",
                None,
                Quality.unpriced,
                "Allocated cost is unavailable because this catalog has no valid baseline allocation policy.",
            ),
        ]
    if relationship == CapacityRelationship.unknown:
        return [
            CalculatedLine(
                "additional", None, Quality.partial, "Execution resource relationship is unknown."
            ),
            CalculatedLine("allocated", None, Quality.partial, "Resource allocation evidence is incomplete."),
        ]
    if interval.observed_start is None or interval.observed_end is None:
        return [
            CalculatedLine("additional", None, Quality.partial, "Resource lifetime is incomplete."),
            CalculatedLine("allocated", None, Quality.partial, "Resource lifetime is incomplete."),
        ]
    prices = price if isinstance(price, list) else ([price] if price is not None else [])
    if not prices:
        return [
            CalculatedLine("additional", None, Quality.unpriced, "No applicable machine price was found."),
            CalculatedLine("allocated", None, Quality.unpriced, "No applicable machine price was found."),
        ]
    seconds = Decimal(str((interval.observed_end - interval.observed_start).total_seconds()))
    if seconds <= 0:
        return [
            CalculatedLine("additional", None, Quality.partial, "Resource lifetime is not positive."),
            CalculatedLine("allocated", None, Quality.partial, "Resource lifetime is not positive."),
        ]
    billed_seconds = max(Decimal("60"), seconds)
    boundaries = sorted({
        value.effective_from for value in prices
        if value.effective_from and interval.observed_start < value.effective_from < interval.observed_end
    })
    points = [interval.observed_start, *boundaries, interval.observed_end]
    allocations: list[dict] = []
    amount = Decimal("0")
    for start, end in zip(points, points[1:], strict=False):
        applicable = [
            value for value in prices
            if value.effective_from is None or value.effective_from <= start
        ]
        active = max(
            applicable,
            key=lambda value: value.effective_from or datetime.min.replace(tzinfo=UTC),
        ) if applicable else None
        if active is None:
            return [
                CalculatedLine("additional", None, Quality.unpriced, "No applicable machine price was found."),
                CalculatedLine("allocated", None, Quality.unpriced, "No applicable machine price was found."),
            ]
        segment_seconds = Decimal(str((end - start).total_seconds()))
        # Distribute one lifetime-level billing minimum over observed duration, including price boundaries.
        charged_seconds = segment_seconds * billed_seconds / seconds
        segment_amount = charged_seconds / Decimal("3600") * active.hourly_rate
        amount += segment_amount
        allocations.append({
            "start": start.isoformat(), "end": end.isoformat(),
            "observed_seconds": str(segment_seconds), "charged_seconds": str(charged_seconds),
            "hourly_rate": str(active.hourly_rate), "amount": str(segment_amount),
            "price_version_id": str(active.id) if active.id else None,
        })
    quality = Quality.approximate if interval.timing_method != "provider_billable" else Quality.complete
    reason = (
        "Dedicated VM compute from observed lifecycle proxy; excludes disk, network, discounts, credits, and taxes."
        if quality == Quality.approximate
        else "Dedicated VM compute from provider billable lifetime."
    )
    return [
        CalculatedLine("additional", amount, quality, reason, billed_seconds, tuple(allocations)),
        CalculatedLine("allocated", amount, quality, reason, billed_seconds, tuple(allocations)),
    ]


def _digest_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _model_facts(rows: list[Any]) -> list[dict[str, Any]]:
    facts = [
        {column.name: _digest_value(getattr(row, column.name)) for column in row.__table__.columns}
        for row in rows
    ]
    return sorted(facts, key=lambda value: json.dumps(value, sort_keys=True, default=_digest_value))


def report_fingerprint(session: Session, tenant_id: uuid.UUID) -> str:
    """Hash every mutable fact that can change a tenant report."""
    tenant = session.get(Tenant, tenant_id)
    owners = list(session.scalars(select(Owner).where(Owner.tenant_id == tenant_id)))
    jobs = list(session.scalars(select(Job).where(Job.tenant_id == tenant_id)))
    attempts = list(session.scalars(
        select(ExecutionAttempt).join(Job).where(Job.tenant_id == tenant_id)
    ))
    intervals = list(session.scalars(
        select(ResourceInterval).join(ExecutionAttempt).join(Job).where(Job.tenant_id == tenant_id)
    ))
    policies = list(session.scalars(select(DeploymentPolicy).where(DeploymentPolicy.tenant_id == tenant_id)))
    prices = list(session.scalars(select(PriceVersion)))
    invocations = list(session.scalars(select(Invocation).where(Invocation.tenant_id == tenant_id)))
    memberships = list(session.scalars(
        select(InvocationJob).join(Invocation).where(Invocation.tenant_id == tenant_id)
    ))
    infrastructure = list(session.scalars(
        select(InfrastructureInterval).where(InfrastructureInterval.tenant_id == tenant_id)
    ))
    facts = {
        "tenant": _model_facts([tenant] if tenant else []),
        "owners": _model_facts(owners),
        "jobs": _model_facts(jobs),
        "attempts": _model_facts(attempts),
        "resource_intervals": _model_facts(intervals),
        "policies": _model_facts(policies),
        "prices": _model_facts(prices),
        "invocations": _model_facts(invocations),
        "invocation_jobs": _model_facts(memberships),
        "infrastructure": _model_facts(infrastructure),
    }
    payload = json.dumps(facts, sort_keys=True, separators=(",", ":"), default=_digest_value)
    return hashlib.sha256(payload.encode()).hexdigest()


def calculate_tenant(
    session: Session, tenant_id: uuid.UUID, reason: str = "fixture ingestion"
) -> CostRevision:
    session.flush()
    rows = session.execute(
        select(ResourceInterval, ExecutionAttempt, Job)
        .join(ExecutionAttempt, ResourceInterval.attempt_id == ExecutionAttempt.id)
        .join(Job, ExecutionAttempt.job_id == Job.id)
        .where(Job.tenant_id == tenant_id)
        .order_by(Job.source_id, ResourceInterval.source_interval_id)
    ).all()
    digest = report_fingerprint(session, tenant_id)
    existing = session.scalar(
        select(CostRevision).where(
            CostRevision.tenant_id == tenant_id,
            CostRevision.calculation_version == CALCULATION_VERSION,
            CostRevision.input_digest == digest,
        )
    )
    if existing:
        return existing

    revision = CostRevision(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        calculation_version=CALCULATION_VERSION,
        input_digest=digest,
        reason=reason,
        created_at=datetime.now(UTC),
    )
    session.add(revision)
    session.flush()
    policy = session.scalar(
        select(DeploymentPolicy)
        .where(DeploymentPolicy.tenant_id == tenant_id)
        .order_by(DeploymentPolicy.effective_from.desc())
    )
    for interval, attempt, job in rows:
        price_statement = select(PriceVersion).where(
                PriceVersion.provider == interval.provider,
                PriceVersion.region == interval.region,
                PriceVersion.machine_type == interval.machine_type,
                PriceVersion.purchase_model == interval.purchase_model,
        )
        if interval.observed_end is not None:
            price_statement = price_statement.where(or_(
                PriceVersion.effective_from.is_(None),
                PriceVersion.effective_from < interval.observed_end,
            ))
        applicable_prices = list(session.scalars(
            price_statement.order_by(PriceVersion.effective_from.asc().nullsfirst())
        ))
        for line in calculate_interval(interval, applicable_prices):
            used_price_ids = {
                allocation["price_version_id"] for allocation in line.allocations
                if allocation["price_version_id"]
            }
            single_price = next(
                (value for value in applicable_prices if str(value.id) in used_price_ids), None
            ) if len(used_price_ids) == 1 else None
            session.add(
                CostLine(
                    id=uuid.uuid4(),
                    revision_id=revision.id,
                    job_id=job.id,
                    attempt_id=attempt.id,
                    resource_interval_id=interval.id,
                    basis=line.basis,
                    component="compute",
                    amount=line.amount,
                    currency=applicable_prices[0].currency if applicable_prices else "USD",
                    quality=line.quality,
                    reason=line.reason,
                    price_version_id=single_price.id if single_price and line.amount is not None else None,
                    policy_id=policy.id if policy else None,
                    details={
                        "machine_type": interval.machine_type,
                        "timing_method": interval.timing_method,
                        "billed_seconds": str(line.billed_seconds) if line.billed_seconds else None,
                        "hourly_rate": str(single_price.hourly_rate) if single_price else None,
                        "allocations": list(line.allocations),
                    },
                )
            )
    session.flush()
    return revision
