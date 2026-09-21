import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from rainstone.models import (
    CapacityRelationship,
    CostLine,
    CostRevision,
    DeploymentPolicy,
    ExecutionAttempt,
    Job,
    PriceVersion,
    Quality,
    ResourceInterval,
)

CALCULATION_VERSION = "phase1-v1"


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
    billed_seconds = max(Decimal("60"), seconds)
    boundaries = sorted({
        value.effective_from for value in prices
        if value.effective_from and interval.observed_start < value.effective_from < interval.observed_end
    })
    points = [interval.observed_start, *boundaries, interval.observed_end]
    allocations: list[dict] = []
    amount = Decimal("0")
    for index, (start, end) in enumerate(zip(points, points[1:], strict=False)):
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
        # Provider minimum uplift is assigned to the first observed segment exactly once.
        charged_seconds = segment_seconds + (billed_seconds - seconds if index == 0 else Decimal("0"))
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


def calculate_tenant(
    session: Session, tenant_id: uuid.UUID, reason: str = "fixture ingestion"
) -> CostRevision:
    rows = session.execute(
        select(ResourceInterval, ExecutionAttempt, Job)
        .join(ExecutionAttempt, ResourceInterval.attempt_id == ExecutionAttempt.id)
        .join(Job, ExecutionAttempt.job_id == Job.id)
        .where(Job.tenant_id == tenant_id)
        .order_by(Job.source_id, ResourceInterval.source_interval_id)
    ).all()
    digest_input = [
        {
            "job": job.source_id,
            "interval": interval.source_interval_id,
            "relationship": interval.capacity_relationship.value,
            "machine": interval.machine_type,
            "region": interval.region,
            "model": interval.purchase_model,
            "start": interval.observed_start.isoformat() if interval.observed_start else None,
            "end": interval.observed_end.isoformat() if interval.observed_end else None,
        }
        for interval, _, job in rows
    ]
    prices = session.scalars(
        select(PriceVersion).order_by(PriceVersion.catalog_id, PriceVersion.machine_type)
    ).all()
    digest_input.extend(
        {
            "catalog": p.catalog_id, "machine": p.machine_type, "rate": str(p.hourly_rate),
            "effective_from": p.effective_from.isoformat() if p.effective_from else None,
        } for p in prices
    )
    digest = hashlib.sha256(json.dumps(digest_input, sort_keys=True).encode()).hexdigest()
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
