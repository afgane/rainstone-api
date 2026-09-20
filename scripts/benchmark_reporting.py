#!/usr/bin/env python3
"""Generate a rolled-back 100k-job corpus and measure representative report calls."""

import argparse
import platform
import statistics
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from rainstone.auth import Identity
from rainstone.db import engine
from rainstone.models import (
    CostLine,
    CostRevision,
    ExecutionAttempt,
    Job,
    Owner,
    Quality,
    ResourceInterval,
    Tenant,
)
from rainstone.report_query import ReportQuery
from rainstone.reporting import list_jobs, summary
from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session

NS = uuid.UUID("547ec231-cf18-41f2-a76d-ae7c96146cbc")


def uid(kind: str, number: int) -> uuid.UUID:
    return uuid.uuid5(NS, f"{kind}:{number}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=100_000)
    parser.add_argument("--output", type=Path, default=Path("docs/benchmark-results.md"))
    args = parser.parse_args()
    started = datetime.now(UTC)
    with Session(engine) as session:
        tenant = session.scalar(select(Tenant).where(Tenant.slug == "anvil-demo"))
        owner = session.scalar(select(Owner).where(Owner.tenant_id == tenant.id, Owner.source_id == "admin"))
        revision_id = uuid.uuid4()
        session.add(CostRevision(
            id=revision_id, tenant_id=tenant.id, calculation_version="phase2a-benchmark",
            input_digest=uuid.uuid4().hex * 2, reason="rolled-back benchmark", created_at=started,
        ))
        session.flush()
        batch_size = 2_000
        for base in range(0, args.jobs, batch_size):
            numbers = range(base, min(args.jobs, base + batch_size))
            jobs, attempts, intervals, lines = [], [], [], []
            for number in numbers:
                job_id, attempt_id, interval_id = uid("job", number), uid("attempt", number), uid("interval", number)
                when = started - timedelta(seconds=number % 2_592_000)
                jobs.append({
                    "id": job_id, "tenant_id": tenant.id, "owner_id": owner.id,
                    "source_id": f"benchmark-{number:06d}", "tool_id": f"benchmark/tool/{number % 10}",
                    "tool_version": f"{number % 4}.0", "state": "ok", "runner": "gcp_batch",
                    "destination": "benchmark", "created_at": when, "updated_at": when,
                    "copied_from_source_id": None,
                })
                attempts.append({
                    "id": attempt_id, "job_id": job_id, "source_attempt_id": "attempt-0",
                    "parent_attempt_id": None, "runner": "gcp_batch", "external_id": f"bench-{number}",
                    "outcome": "ok", "tool_started_at": when, "tool_finished_at": when + timedelta(seconds=60),
                })
                intervals.append({
                    "id": interval_id, "attempt_id": attempt_id, "source_interval_id": "interval-0",
                    "resource_uid": f"bench-vm-{number}", "provider": "gcp", "region": "us-central1",
                    "machine_type": "n2-standard-2", "purchase_model": "on_demand",
                    "capacity_relationship": "dedicated", "observed_start": when,
                    "observed_end": when + timedelta(seconds=60), "timing_method": "benchmark",
                    "requested_vcpu": Decimal("2"), "requested_memory_mib": Decimal("8192"), "facts": {},
                })
                lines.append({
                    "id": uid("line", number), "revision_id": revision_id, "job_id": job_id,
                    "attempt_id": attempt_id, "resource_interval_id": interval_id, "basis": "additional",
                    "component": "compute", "amount": Decimal("0.001618633333"), "currency": "USD",
                    "quality": Quality.complete, "reason": "Synthetic reporting benchmark.",
                    "price_version_id": None, "policy_id": None, "details": {},
                })
            session.execute(insert(Job), jobs)
            session.execute(insert(ExecutionAttempt), attempts)
            session.execute(insert(ResourceInterval), intervals)
            session.execute(insert(CostLine), lines)
            session.flush()
        identity = Identity(tenant.id, owner.id, owner.source_id, owner.label, True)
        query = ReportQuery(search="benchmark/tool/3", runner="gcp_batch", limit=50, sort="amount")
        timings: dict[str, list[float]] = {"summary": [], "first_page": [], "deep_page": []}
        for _iteration in range(8):
            for name, call in (
                ("summary", lambda: summary(session, identity, query)),
                ("first_page", lambda: list_jobs(session, identity, query)),
                ("deep_page", lambda: list_jobs(session, identity, query.model_copy(update={"offset": 9_900}))),
            ):
                before = time.perf_counter()
                call()
                timings[name].append((time.perf_counter() - before) * 1000)
        generation_seconds = (datetime.now(UTC) - started).total_seconds()
        rows = [
            "# Rainstone Phase 2A reporting benchmark", "",
            f"- Generated jobs: {args.jobs:,} (transaction rolled back)",
            f"- Host: {platform.platform()} · {platform.machine()} · Python {platform.python_version()}",
            f"- PostgreSQL URL host: {engine.url.host}",
            f"- Corpus generation: {generation_seconds:.2f} s",
            "- Query: runner=gcp_batch, search=benchmark/tool/3 (10,000 matching jobs), amount sort",
            "- Eight iterations; first is cold with respect to application objects, later runs are warm.", "",
            "| Request | Cold | Warm p50 | Warm p95 | Maximum |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for name, values in timings.items():
            warm = values[1:]
            ordered = sorted(warm)
            p95 = ordered[min(len(ordered) - 1, round(.95 * (len(ordered) - 1)))]
            rows.append(
                f"| {name} | {values[0]:.1f} ms | {statistics.median(warm):.1f} ms "
                f"| {p95:.1f} ms | {max(values):.1f} ms |"
            )
        plan = session.execute(text(
            "EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM job JOIN owner ON job.owner_id=owner.id "
            "WHERE job.tenant_id=:tenant AND job.runner='gcp_batch' "
            "AND (job.source_id ILIKE '%benchmark/tool/3%' OR job.tool_id ILIKE '%benchmark/tool/3%' "
            "OR coalesce(job.tool_version, '') ILIKE '%benchmark/tool/3%' "
            "OR owner.label ILIKE '%benchmark/tool/3%')"
        ), {"tenant": tenant.id}).scalars().all()
        rows += [
            "", "Target: ordinary interactive requests below 2,000 ms.", "",
            "## Representative database plan", "",
            "The leading-wildcard shared search uses a sequential scan at this scale; total request time above "
            "also includes authorization, cost-line loading, aggregation, stable sorting, and serialization.",
            "", "```text", *plan, "```", "",
        ]
        args.output.write_text("\n".join(rows))
        session.rollback()
        print(args.output)


if __name__ == "__main__":
    main()
