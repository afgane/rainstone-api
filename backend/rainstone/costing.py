import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from rainstone.models import (
    CapacityRelationship,
    CostLine,
    CostRevision,
    DeploymentPolicy,
    ExecutionAttempt,
    Job,
    LifetimeAttempt,
    PriceVersion,
    Quality,
    ReportGeneration,
    ResourceLifetime,
    ResourceSegment,
)

CALCULATION_VERSION = "phase2b-v2"
MINIMUM_BILLED_SECONDS = Decimal("60")


@dataclass(frozen=True)
class CalculatedLine:
    basis: str
    amount: Decimal | None
    quality: Quality
    reason: str
    billed_seconds: Decimal | None = None
    allocations: tuple[dict, ...] = ()


def _unavailable(reason: str, quality: Quality = Quality.partial) -> list[CalculatedLine]:
    return [
        CalculatedLine("additional", None, quality, reason),
        CalculatedLine("allocated", None, quality, reason),
    ]


def _windows(
    lifetime: ResourceLifetime, segments: Sequence[ResourceSegment]
) -> list[tuple[datetime, datetime]]:
    """Positive-duration accounting windows for one lifetime."""
    pairs: list[tuple[datetime, datetime]] = []
    for segment in segments:
        if segment.observed_start and segment.observed_end:
            pairs.append((segment.observed_start, segment.observed_end))
    if not pairs and lifetime.observed_start and lifetime.observed_end:
        pairs.append((lifetime.observed_start, lifetime.observed_end))
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(pairs):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _active_price(prices: Sequence[PriceVersion], at: datetime) -> PriceVersion | None:
    applicable = [
        price for price in prices if price.effective_from is None or price.effective_from <= at
    ]
    if not applicable:
        return None
    return max(
        applicable, key=lambda price: price.effective_from or datetime.min.replace(tzinfo=UTC)
    )


def calculate_lifetime(
    lifetime: ResourceLifetime,
    segments: Sequence[ResourceSegment],
    price: PriceVersion | Sequence[PriceVersion] | None,
    *,
    shared_job_count: int = 1,
) -> list[CalculatedLine]:
    """Price one chargeable resource lifetime once per basis.

    The provider minimum applies to the lifetime, not to each attempt or each
    daily bucket, and its uplift is distributed proportionally across the
    observed positive-duration windows.
    """
    relationship = lifetime.capacity_relationship
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
    if shared_job_count > 1:
        return _unavailable(
            "More than one Galaxy job used this resource; an explicit allocation policy and "
            "occupancy coverage are required before charging it.",
        )
    windows = _windows(lifetime, segments)
    if not windows:
        return _unavailable("Resource lifetime is incomplete.")
    prices = list(price) if isinstance(price, Sequence) else ([price] if price is not None else [])
    if not prices:
        return _unavailable("No applicable machine price was found.", Quality.unpriced)
    observed = sum(
        (Decimal(str((end - start).total_seconds())) for start, end in windows), Decimal("0")
    )
    if observed <= 0:
        return _unavailable("Resource lifetime is not positive.")
    billed_seconds = max(MINIMUM_BILLED_SECONDS, observed)
    allocations: list[dict] = []
    amount = Decimal("0")
    for window_start, window_end in windows:
        boundaries = sorted(
            {
                value.effective_from
                for value in prices
                if value.effective_from and window_start < value.effective_from < window_end
            }
        )
        points = [window_start, *boundaries, window_end]
        for start, end in zip(points, points[1:], strict=False):
            active = _active_price(prices, start)
            if active is None:
                earliest = min(value.effective_from for value in prices if value.effective_from)
                return _unavailable(
                    "No published price covers this machine and region when it ran: the "
                    f"earliest takes effect {earliest.isoformat()}, and a later price is "
                    "never applied to earlier work.",
                    Quality.unpriced,
                )
            segment_seconds = Decimal(str((end - start).total_seconds()))
            charged_seconds = segment_seconds * billed_seconds / observed
            segment_amount = charged_seconds / Decimal("3600") * active.hourly_rate
            amount += segment_amount
            allocations.append(
                {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "observed_seconds": str(segment_seconds),
                    "charged_seconds": str(charged_seconds),
                    "hourly_rate": str(active.hourly_rate),
                    "amount": str(segment_amount),
                    "price_version_id": str(active.id) if active.id else None,
                }
            )
    quality = Quality.complete if lifetime.timing_method == "provider_billable" else Quality.approximate
    reason = (
        "Dedicated VM compute from provider billable lifetime."
        if quality == Quality.complete
        else "Dedicated VM compute from observed lifecycle proxy; excludes disk, network, "
        "discounts, credits, and taxes."
    )
    return [
        CalculatedLine("additional", amount, quality, reason, billed_seconds, tuple(allocations)),
        CalculatedLine("allocated", amount, quality, reason, billed_seconds, tuple(allocations)),
    ]


# Change detection runs in the database: hashing every fact row in Python cost
# a multiple of the job count on each request. These are change markers, not
# security digests, so md5 of the row text is sufficient.
FINGERPRINT = text("""
    WITH parts AS (
        SELECT md5(t::text) AS h FROM tenant t WHERE t.id = :tenant
        UNION ALL SELECT md5(t::text) FROM owner t WHERE t.tenant_id = :tenant
        UNION ALL SELECT md5(t::text) FROM job t WHERE t.tenant_id = :tenant
        UNION ALL SELECT md5(t::text)
                    FROM execution_attempt t
                    JOIN job j ON j.id = t.job_id
                   WHERE j.tenant_id = :tenant
        UNION ALL SELECT md5(t::text) FROM resource_lifetime t WHERE t.tenant_id = :tenant
        UNION ALL SELECT md5(t::text)
                    FROM resource_segment t
                    JOIN resource_lifetime l ON l.id = t.lifetime_id
                   WHERE l.tenant_id = :tenant
        UNION ALL SELECT md5(t::text)
                    FROM lifetime_attempt t
                    JOIN resource_lifetime l ON l.id = t.lifetime_id
                   WHERE l.tenant_id = :tenant
        UNION ALL SELECT md5(t::text) FROM deployment_policy t WHERE t.tenant_id = :tenant
        UNION ALL SELECT md5(t::text) FROM price_version t
        UNION ALL SELECT md5(t::text) FROM invocation t WHERE t.tenant_id = :tenant
        UNION ALL SELECT md5(t::text)
                    FROM invocation_job t
                    JOIN invocation i ON i.id = t.invocation_id
                   WHERE i.tenant_id = :tenant
        UNION ALL SELECT md5(t::text)
                    FROM infrastructure_interval t
                   WHERE t.tenant_id = :tenant
    )
    SELECT coalesce(md5(string_agg(h, '' ORDER BY h)), 'empty') AS digest FROM parts
""")


def report_fingerprint(session: Session, tenant_id: uuid.UUID) -> str:
    """Hash the mutable facts that can change a tenant report.

    This identifies *identical* facts, so a replay that rewrites the same
    values reuses its calculation revision. Request-time staleness uses the
    cheaper generation marker below. Raw job metrics are deliberately absent:
    they reach reports only through the attempts, lifetimes and job resource
    hints hashed here.
    """
    session.flush()
    return session.execute(FINGERPRINT, {"tenant": tenant_id}).scalar_one()


def current_generation(session: Session, tenant_id: uuid.UUID) -> int:
    """The tenant's report-generation marker, advanced by database triggers.

    Reporting requests only read it; the marker row is created when the tenant
    is, so a missing row means no facts have been recorded yet.
    """
    session.flush()
    row = session.get(ReportGeneration, tenant_id)
    if row is None:
        return 0
    session.refresh(row)
    return row.generation


def _ensure_generation(session: Session, tenant_id: uuid.UUID) -> int:
    row = session.get(ReportGeneration, tenant_id)
    if row is None:
        row = ReportGeneration(tenant_id=tenant_id, generation=1, updated_at=datetime.now(UTC))
        session.add(row)
        session.flush()
    return current_generation(session, tenant_id)


def _applicable_prices(session: Session, lifetime: ResourceLifetime) -> list[PriceVersion]:
    """Every price for the lifetime's shape; only those in effect while it ran apply.

    Prices that take effect later are kept so an unpriced lifetime can say that
    its shape is priced, just not for when it ran.
    """
    statement = select(PriceVersion).where(
        PriceVersion.provider == lifetime.provider,
        PriceVersion.region == lifetime.region,
        PriceVersion.machine_type == lifetime.machine_type,
        PriceVersion.purchase_model == lifetime.purchase_model,
    )
    return list(session.scalars(statement.order_by(PriceVersion.effective_from.asc().nullsfirst())))


def calculate_tenant(
    session: Session, tenant_id: uuid.UUID, reason: str = "collection"
) -> CostRevision:
    session.flush()
    generation = _ensure_generation(session, tenant_id)
    latest = session.scalar(
        select(CostRevision)
        .where(
            CostRevision.tenant_id == tenant_id,
            CostRevision.calculation_version == CALCULATION_VERSION,
        )
        .order_by(CostRevision.created_at.desc(), CostRevision.id.desc())
    )
    if latest is not None and latest.facts_generation == generation:
        return latest
    digest = report_fingerprint(session, tenant_id)
    existing = session.scalar(
        select(CostRevision).where(
            CostRevision.tenant_id == tenant_id,
            CostRevision.calculation_version == CALCULATION_VERSION,
            CostRevision.input_digest == digest,
        )
    )
    if existing:
        # Replaying identical facts keeps the same revision; only its marker
        # moves forward, because the rewrite advanced the generation.
        existing.facts_generation = generation
        session.flush()
        return existing

    revision = CostRevision(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        calculation_version=CALCULATION_VERSION,
        input_digest=digest,
        facts_generation=generation,
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
    lifetimes = list(
        session.scalars(
            select(ResourceLifetime)
            .where(ResourceLifetime.tenant_id == tenant_id)
            .order_by(ResourceLifetime.resource_key)
        )
    )
    for lifetime in lifetimes:
        segments = list(
            session.scalars(
                select(ResourceSegment)
                .where(ResourceSegment.lifetime_id == lifetime.id)
                .order_by(ResourceSegment.source_segment_id)
            )
        )
        links = list(
            session.execute(
                select(LifetimeAttempt, ExecutionAttempt, Job)
                .join(ExecutionAttempt, LifetimeAttempt.attempt_id == ExecutionAttempt.id)
                .join(Job, ExecutionAttempt.job_id == Job.id)
                .where(LifetimeAttempt.lifetime_id == lifetime.id)
                .order_by(Job.source_id, ExecutionAttempt.source_attempt_id)
            ).all()
        )
        if not links:
            continue
        jobs = {job.id: job for _, _, job in links}
        prices = _applicable_prices(session, lifetime)
        lines = calculate_lifetime(lifetime, segments, prices, shared_job_count=len(jobs))
        for job_id, job in jobs.items():
            attempts = [
                attempt for _, attempt, attempt_job in links if attempt_job.id == job_id
            ]
            for line in lines:
                used_price_ids = {
                    allocation["price_version_id"]
                    for allocation in line.allocations
                    if allocation["price_version_id"]
                }
                single_price = (
                    next((value for value in prices if str(value.id) in used_price_ids), None)
                    if len(used_price_ids) == 1
                    else None
                )
                session.add(
                    CostLine(
                        id=uuid.uuid4(),
                        revision_id=revision.id,
                        job_id=job.id,
                        lifetime_id=lifetime.id,
                        # A charge shared by retries belongs to the lifetime, not
                        # to one attempt row.
                        attempt_id=attempts[0].id if len(attempts) == 1 else None,
                        basis=line.basis,
                        component="compute",
                        amount=line.amount,
                        currency=prices[0].currency if prices else "USD",
                        quality=line.quality,
                        reason=line.reason,
                        price_version_id=single_price.id if single_price and line.amount is not None else None,
                        policy_id=policy.id if policy else None,
                        details={
                            "resource_key": lifetime.resource_key,
                            "resource_uid": lifetime.resource_uid,
                            "machine_type": lifetime.machine_type,
                            "timing_method": lifetime.timing_method,
                            "billed_seconds": str(line.billed_seconds) if line.billed_seconds else None,
                            "hourly_rate": str(single_price.hourly_rate) if single_price else None,
                            "shared_attempt_ids": [str(attempt.id) for attempt in attempts],
                            "shared_attempt_count": len(attempts),
                            "shared_job_count": len(jobs),
                            "allocations": list(line.allocations),
                        },
                    )
                )
    session.flush()
    return revision
