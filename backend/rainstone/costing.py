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


def calculate_interval(interval: ResourceInterval, price: PriceVersion | None) -> list[CalculatedLine]:
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
    if price is None:
        return [
            CalculatedLine("additional", None, Quality.unpriced, "No applicable machine price was found."),
            CalculatedLine("allocated", None, Quality.unpriced, "No applicable machine price was found."),
        ]
    seconds = Decimal(str((interval.observed_end - interval.observed_start).total_seconds()))
    billed_seconds = max(Decimal("60"), seconds)
    amount = billed_seconds / Decimal("3600") * price.hourly_rate
    quality = Quality.approximate if interval.timing_method != "provider_billable" else Quality.complete
    reason = (
        "Dedicated VM compute from observed lifecycle proxy; excludes disk, network, discounts, credits, and taxes."
        if quality == Quality.approximate
        else "Dedicated VM compute from provider billable lifetime."
    )
    return [
        CalculatedLine("additional", amount, quality, reason, billed_seconds),
        CalculatedLine("allocated", amount, quality, reason, billed_seconds),
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
        {"catalog": p.catalog_id, "machine": p.machine_type, "rate": str(p.hourly_rate)} for p in prices
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
        if interval.observed_start is None:
            price_statement = price_statement.where(PriceVersion.effective_from.is_(None))
        else:
            price_statement = price_statement.where(
                or_(
                    PriceVersion.effective_from.is_(None),
                    PriceVersion.effective_from <= interval.observed_start,
                )
            )
        price = session.scalar(
            price_statement.order_by(PriceVersion.effective_from.desc().nullslast()).limit(1)
        )
        for line in calculate_interval(interval, price):
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
                    currency=price.currency if price else "USD",
                    quality=line.quality,
                    reason=line.reason,
                    price_version_id=price.id if price and line.amount is not None else None,
                    policy_id=policy.id if policy else None,
                    details={
                        "machine_type": interval.machine_type,
                        "timing_method": interval.timing_method,
                        "billed_seconds": str(line.billed_seconds) if line.billed_seconds else None,
                        "hourly_rate": str(price.hourly_rate) if price else None,
                    },
                )
            )
    session.flush()
    return revision
