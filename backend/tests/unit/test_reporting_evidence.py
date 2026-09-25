"""How reports reconcile execution evidence and place it in time."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from rainstone.models import ExecutionAttempt
from rainstone.report_query import ReportQuery
from rainstone.reporting import _executions, _quality, _slice_fraction

START = datetime(2026, 9, 23, 1, tzinfo=UTC)


def attempt(
    source_attempt_id: str,
    *,
    task: int | None = None,
    ordinal: int | None = None,
    offset_minutes: int = 0,
) -> ExecutionAttempt:
    return ExecutionAttempt(
        id=uuid.uuid4(),
        source_attempt_id=source_attempt_id,
        runner="gcp_batch",
        outcome="ok",
        task_index=task,
        attempt_ordinal=ordinal,
        tool_started_at=START + timedelta(minutes=offset_minutes),
    )


def test_galaxy_and_provider_views_of_one_execution_are_one_attempt() -> None:
    galaxy = attempt("galaxy-0")
    provider = attempt("batch:uid:task-0:attempt-0", task=0, ordinal=0)
    first, repeats, evidence = _executions([galaxy, provider], set())
    assert first == [provider]
    assert repeats == []
    assert evidence == "provider"


def test_a_later_ordinal_of_the_same_task_is_a_repeat() -> None:
    original = attempt("batch:uid:task-0:attempt-0", task=0, ordinal=0)
    retry = attempt("batch:uid:task-0:attempt-1", task=0, ordinal=1, offset_minutes=5)
    first, repeats, _ = _executions([attempt("galaxy-0"), retry, original], set())
    assert first == [original]
    assert repeats == [retry]


def test_parallel_tasks_are_not_repeats() -> None:
    tasks = [attempt(f"batch:uid:task-{index}:attempt-0", task=index, ordinal=0) for index in (0, 1)]
    first, repeats, _ = _executions(tasks, set())
    assert sorted(a.task_index for a in first) == [0, 1]
    assert repeats == []


def test_a_galaxy_record_with_its_own_resource_is_a_separate_attempt() -> None:
    earlier = attempt("galaxy-0")
    later = attempt("galaxy-1-batch-task-0", offset_minutes=5)
    first, repeats, _ = _executions([earlier, later], {earlier.id})
    assert first == [earlier]
    assert repeats == [later]


def test_only_galaxy_evidence_is_named_as_such() -> None:
    first, repeats, evidence = _executions([attempt("galaxy-0")], set())
    assert (len(first), repeats, evidence) == (1, [], "galaxy_record")
    assert _executions([], set()) == ([], [], "none")


def test_missing_cost_evidence_follows_execution_state() -> None:
    assert _quality([], "running") == "in_progress"
    assert _quality([], "queued") == "in_progress"
    assert _quality([], "paused") == "not_started"
    assert _quality([], "ok") == "unavailable"
    assert _quality([], "error") == "unavailable"


def test_an_instant_belongs_whole_to_the_interval_containing_it() -> None:
    day = ReportQuery(from_time=START.replace(hour=0), to_time=START.replace(hour=0) + timedelta(days=1))
    next_day = ReportQuery(from_time=day.to_time, to_time=day.to_time + timedelta(days=1))
    assert _slice_fraction(START, START, day) == Decimal("1")
    assert _slice_fraction(START, START, next_day) == Decimal("0")
    # The interval is half-open: its end instant belongs to the next one.
    assert _slice_fraction(day.to_time, day.to_time, day) == Decimal("0")
    assert _slice_fraction(START, START, ReportQuery()) == Decimal("1")
