"""Bounded, allowlisted Galaxy database extraction.

Every statement names its columns explicitly, runs in a read-only transaction
with a statement timeout, and is restricted to logical job facts. Command lines,
stdout/stderr, tracebacks, credentials, dataset contents and the pickled
`destination_params` blob are never selected. Resource hints come from
allowlisted numeric metrics rather than from that blob, so column-level grants
remain meaningful.

Execution-resource evidence is joined later by runner adapters; this adapter
only reports what Galaxy itself recorded.
"""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from rainstone.adapters.contracts import (
    NormalizedAttempt,
    NormalizedInvocation,
    NormalizedJob,
    NormalizedMembership,
    NormalizedMetric,
    NormalizedOwner,
    NormalizedStateEvent,
    ObservationBatch,
)

SOURCE_NAME = "galaxy_db"

TERMINAL_STATES = ("ok", "error", "deleted", "deleting", "failed")

REQUIRED_TABLES: dict[str, tuple[str, ...]] = {
    "job": (
        "id",
        "create_time",
        "update_time",
        "user_id",
        "history_id",
        "tool_id",
        "tool_version",
        "state",
        "exit_code",
        "handler",
        "job_runner_name",
        "job_runner_external_id",
        "destination_id",
        "copied_from_job_id",
    ),
    "job_state_history": ("job_id", "state", "create_time"),
    "job_metric_numeric": ("job_id", "plugin", "metric_name", "metric_value"),
    "galaxy_user": ("id", "username"),
    "history": ("id", "user_id"),
    "workflow_invocation": ("id", "workflow_id", "history_id", "state", "create_time"),
    "workflow_invocation_step": (
        "id",
        "workflow_invocation_id",
        "workflow_step_id",
        "job_id",
        "implicit_collection_jobs_id",
    ),
    "workflow_invocation_to_subworkflow_invocation_association": (
        "workflow_invocation_id",
        "subworkflow_invocation_id",
    ),
    "implicit_collection_jobs_job_association": ("implicit_collection_jobs_id", "job_id"),
    "workflow": ("id", "stored_workflow_id", "name"),
}

# Numeric metrics Rainstone is allowed to read, with the units Galaxy records.
METRIC_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "core": ("galaxy_slots", "galaxy_memory_mb", "runtime_seconds", "start_epoch", "end_epoch"),
    "cgroup": ("cpu.stat.usage_usec", "memory.peak", "memory.max_usage_in_bytes"),
}

_JOB_COLUMNS = """
           j.id::text AS source_id,
           j.user_id::text AS owner_source_id,
           j.tool_id,
           j.tool_version,
           j.state,
           j.exit_code,
           j.handler,
           j.job_runner_name AS runner,
           j.job_runner_external_id AS external_id,
           j.destination_id AS destination,
           j.copied_from_job_id::text AS copied_from_source_id,
           j.create_time AS created_at,
           j.update_time AS updated_at
"""

BACKFILL_PAGE = text(f"""
    SELECT {_JOB_COLUMNS}
      FROM job j
     WHERE j.id > :id_after AND j.id <= :id_ceiling
     ORDER BY j.id
     LIMIT :batch_size
""")

INCREMENTAL_PAGE = text(f"""
    SELECT {_JOB_COLUMNS}
      FROM job j
     WHERE (j.update_time, j.id) > (:updated_after, :id_after)
     ORDER BY j.update_time, j.id
     LIMIT :batch_size
""")

JOBS_BY_ID = text(f"""
    SELECT {_JOB_COLUMNS}
      FROM job j
     WHERE j.id = ANY(:ids)
     ORDER BY j.id
""")

NONTERMINAL_IDS = text("""
    SELECT j.id
      FROM job j
     WHERE j.state <> ALL(:terminal_states)
     ORDER BY j.id DESC
     LIMIT :batch_size
""")

RECENTLY_TERMINAL_IDS = text("""
    SELECT j.id
      FROM job j
     WHERE j.state = ANY(:terminal_states) AND j.update_time >= :since
     ORDER BY j.id DESC
     LIMIT :batch_size
""")

SWEEP_IDS = text("""
    SELECT j.id
      FROM job j
     WHERE j.id > :id_after
     ORDER BY j.id
     LIMIT :batch_size
""")

MAX_JOB_ID = text("SELECT coalesce(max(j.id), 0) AS value FROM job j")

METRICS = text("""
    SELECT m.job_id::text AS job_source_id, m.plugin, m.metric_name, m.metric_value
      FROM job_metric_numeric m
     WHERE m.job_id = ANY(:ids)
       AND m.plugin = ANY(:plugins)
       AND m.metric_name = ANY(:names)
""")

STATE_EVENTS = text("""
    SELECT h.job_id::text AS job_source_id, h.state, h.create_time AS occurred_at
      FROM job_state_history h
     WHERE h.job_id = ANY(:ids)
     ORDER BY h.job_id, h.create_time
""")

OWNERS = text("""
    SELECT u.id::text AS source_id, u.username
      FROM galaxy_user u
     WHERE u.id = ANY(:ids)
""")

# The tree is collected separately from membership, because a root invocation
# whose steps are all subworkflows contributes no jobs of its own and must
# still be recorded before its children.
INVOCATION_TREE = text("""
    WITH RECURSIVE roots AS (
        SELECT wi.id
          FROM workflow_invocation wi
         WHERE NOT EXISTS (
                   SELECT 1
                     FROM workflow_invocation_to_subworkflow_invocation_association a
                    WHERE a.subworkflow_invocation_id = wi.id
               )
           AND (CAST(:root_ids AS bigint[]) IS NULL OR wi.id = ANY(:root_ids))
    ), tree AS (
        SELECT r.id AS root_id, r.id AS invocation_id,
               NULL::bigint AS parent_id, 0 AS depth, ARRAY[r.id] AS path
          FROM roots r
        UNION ALL
        SELECT t.root_id, a.subworkflow_invocation_id, t.invocation_id, t.depth + 1,
               t.path || a.subworkflow_invocation_id
          FROM tree t
          JOIN workflow_invocation_to_subworkflow_invocation_association a
            ON a.workflow_invocation_id = t.invocation_id
         WHERE NOT a.subworkflow_invocation_id = ANY(t.path)
    )
    SELECT t.root_id::text AS root_source_id,
           t.invocation_id::text AS invocation_source_id,
           t.parent_id::text AS parent_source_id,
           t.depth,
           wi.state,
           wi.create_time AS created_at,
           wi.workflow_id::text AS workflow_id,
           w.stored_workflow_id::text AS workflow_family_id,
           w.name AS workflow_name,
           h.user_id::text AS invocation_owner_source_id
      FROM tree t
      JOIN workflow_invocation wi ON wi.id = t.invocation_id
      JOIN history h ON h.id = wi.history_id
      LEFT JOIN workflow w ON w.id = wi.workflow_id
     ORDER BY t.depth, t.invocation_id
""")

# Recursive invocation attribution proven against the AnVIL dev instance:
# root invocations expand through subworkflow associations, and each step
# contributes direct jobs plus implicit-collection expansions. Invocation
# ownership comes from its history, because this schema has no
# `workflow_invocation.user_id` column.
INVOCATION_MEMBERSHIP = text("""
    WITH RECURSIVE roots AS (
        SELECT wi.id
          FROM workflow_invocation wi
         WHERE NOT EXISTS (
                   SELECT 1
                     FROM workflow_invocation_to_subworkflow_invocation_association a
                    WHERE a.subworkflow_invocation_id = wi.id
               )
           AND (CAST(:root_ids AS bigint[]) IS NULL OR wi.id = ANY(:root_ids))
    ), tree AS (
        SELECT r.id AS root_id, r.id AS invocation_id,
               NULL::bigint AS parent_id, ARRAY[r.id] AS path
          FROM roots r
        UNION ALL
        SELECT t.root_id, a.subworkflow_invocation_id, t.invocation_id,
               t.path || a.subworkflow_invocation_id
          FROM tree t
          JOIN workflow_invocation_to_subworkflow_invocation_association a
            ON a.workflow_invocation_id = t.invocation_id
         WHERE NOT a.subworkflow_invocation_id = ANY(t.path)
    ), membership AS (
        SELECT t.root_id, t.invocation_id, t.parent_id,
               s.id AS step_id, s.workflow_step_id, s.job_id, 'direct' AS relationship
          FROM tree t
          JOIN workflow_invocation_step s ON s.workflow_invocation_id = t.invocation_id
         WHERE s.job_id IS NOT NULL
        UNION ALL
        SELECT t.root_id, t.invocation_id, t.parent_id,
               s.id, s.workflow_step_id, a.job_id, 'collection'
          FROM tree t
          JOIN workflow_invocation_step s ON s.workflow_invocation_id = t.invocation_id
          JOIN implicit_collection_jobs_job_association a
            ON a.implicit_collection_jobs_id = s.implicit_collection_jobs_id
    )
    SELECT m.root_id::text AS root_source_id,
           m.invocation_id::text AS invocation_source_id,
           m.parent_id::text AS parent_source_id,
           m.step_id::text AS step_id,
           m.workflow_step_id::text AS workflow_step_id,
           m.job_id::text AS job_source_id,
           m.relationship,
           wi.state,
           wi.create_time AS created_at,
           wi.workflow_id::text AS workflow_id,
           w.stored_workflow_id::text AS workflow_family_id,
           w.name AS workflow_name,
           h.user_id::text AS invocation_owner_source_id,
           j.user_id::text AS job_owner_source_id
      FROM membership m
      JOIN workflow_invocation wi ON wi.id = m.invocation_id
      JOIN history h ON h.id = wi.history_id
      LEFT JOIN workflow w ON w.id = wi.workflow_id
      JOIN job j ON j.id = m.job_id
     ORDER BY m.root_id, m.invocation_id, m.step_id, m.job_id
""")

INVOCATION_ROOTS_FOR_JOBS = text("""
    WITH RECURSIVE owning AS (
        SELECT s.workflow_invocation_id AS invocation_id
          FROM workflow_invocation_step s
         WHERE s.job_id = ANY(:ids)
        UNION
        SELECT s.workflow_invocation_id
          FROM workflow_invocation_step s
          JOIN implicit_collection_jobs_job_association a
            ON a.implicit_collection_jobs_id = s.implicit_collection_jobs_id
         WHERE a.job_id = ANY(:ids)
    ), up AS (
        SELECT o.invocation_id FROM owning o
        UNION
        SELECT a.workflow_invocation_id
          FROM up u
          JOIN workflow_invocation_to_subworkflow_invocation_association a
            ON a.subworkflow_invocation_id = u.invocation_id
    )
    SELECT DISTINCT u.invocation_id
      FROM up u
     WHERE NOT EXISTS (
               SELECT 1
                 FROM workflow_invocation_to_subworkflow_invocation_association a
                WHERE a.subworkflow_invocation_id = u.invocation_id
           )
""")

ACTIVE_INVOCATION_ROOTS = text("""
    SELECT wi.id
      FROM workflow_invocation wi
     WHERE (wi.state <> ALL(ARRAY['scheduled', 'cancelled', 'failed'])
            OR wi.update_time >= :since)
       AND NOT EXISTS (
               SELECT 1
                 FROM workflow_invocation_to_subworkflow_invocation_association a
                WHERE a.subworkflow_invocation_id = wi.id
           )
     ORDER BY wi.id DESC
     LIMIT :batch_size
""")

SETTLED_INVOCATION_STEPS = text("""
    SELECT wi.id::text AS invocation_source_id,
           count(*) FILTER (
               WHERE s.state IS NULL OR s.state <> 'scheduled'
           ) AS unscheduled_steps
      FROM workflow_invocation wi
      JOIN workflow_invocation_step s ON s.workflow_invocation_id = wi.id
     WHERE wi.id = ANY(:ids)
     GROUP BY wi.id
""")

VERSION_PROBE = text("SELECT version_num FROM alembic_version LIMIT 1")

# Rainstone writes nothing to the source database, so it has no identifier of
# its own there. The oldest job row is corroborating evidence that the endpoint
# still holds the database this instance enrolled with. It is a replacement
# alarm, never proof of identity: a restored copy carries the same oldest job,
# and a read-only client cannot observe the deployment event behind an endpoint.
SOURCE_FINGERPRINT_PROBE = text("SELECT id, create_time FROM job ORDER BY id LIMIT 1")

SCHEMA_PROBE = text("""
    SELECT table_name, column_name
      FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name = ANY(:tables)
""")


@dataclass(frozen=True)
class GalaxyCapabilities:
    """What this connection can read, plus evidence about which database it is.

    `schema_fingerprint` reflects the columns *this* role can see, so an
    administrator and a column-restricted reader legitimately disagree; it is a
    capability record, not identity. `source_fingerprint` is corroborating
    evidence only: enrollment identity lives in Rainstone's own database.
    """

    compatible: bool
    schema_fingerprint: str
    source_version: str | None
    source_fingerprint: str | None = None
    fingerprint_status: str = "unknown"
    missing: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)


def read_source_fingerprint(engine: Engine, *, statement_timeout: str = "30s") -> tuple[str | None, str]:
    with engine.connect() as connection:
        _read_only(connection, statement_timeout)
        return _fingerprint_in_savepoint(connection)


def _fingerprint_in_savepoint(connection: Connection) -> tuple[str | None, str]:
    """Probe inside a savepoint, so a denial does not abort the enclosing work."""
    try:
        with connection.begin_nested():
            return _read_source_fingerprint(connection), "present"
    except _NoJobRows:
        # A Galaxy that has never run a job is legitimately unfingerprintable.
        return None, "empty"
    except Exception as error:  # noqa: BLE001 - classified for diagnostics
        denied = "permission denied" in str(error).lower()
        return None, "denied" if denied else "unavailable"


class _NoJobRows(Exception):
    """The source has no job rows to fingerprint yet."""


def _read_source_fingerprint(connection: Connection) -> str:
    """Hash the oldest job row: the same database keeps the same oldest job."""
    row = connection.execute(SOURCE_FINGERPRINT_PROBE).mappings().first()
    if row is None:
        raise _NoJobRows
    created = _utc(row["create_time"])
    material = f"{row['id']}:{created.isoformat() if created else ''}"
    return hashlib.sha256(material.encode()).hexdigest()


def _read_only(connection: Connection, statement_timeout: str) -> None:
    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
    connection.exec_driver_sql(f"SET LOCAL statement_timeout = '{statement_timeout}'")


def _utc(value: datetime | None) -> datetime | None:
    """Galaxy stores naive UTC timestamps; reporting stores aware UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def discover_capabilities(engine: Engine, *, statement_timeout: str = "30s") -> GalaxyCapabilities:
    with engine.connect() as connection:
        _read_only(connection, statement_timeout)
        source_fingerprint, fingerprint_status = _fingerprint_in_savepoint(connection)
        present: dict[str, set[str]] = {}
        for row in connection.execute(
            SCHEMA_PROBE, {"tables": list(REQUIRED_TABLES)}
        ).mappings():
            present.setdefault(row["table_name"], set()).add(row["column_name"])
        try:
            version = connection.execute(VERSION_PROBE).scalar()
        except Exception:  # noqa: BLE001 - version probe is best effort
            version = None
    missing: list[str] = []
    for table, columns in REQUIRED_TABLES.items():
        if table not in present:
            missing.append(table)
            continue
        missing.extend(f"{table}.{column}" for column in columns if column not in present[table])
    listing = ";".join(
        f"{table}:{','.join(sorted(present.get(table, ())))}" for table in sorted(REQUIRED_TABLES)
    )
    fingerprint = hashlib.sha256(listing.encode()).hexdigest()
    return GalaxyCapabilities(
        compatible=not missing,
        schema_fingerprint=fingerprint,
        source_version=version,
        source_fingerprint=source_fingerprint,
        fingerprint_status=fingerprint_status,
        missing=tuple(missing),
        details={"tables": {table: sorted(columns) for table, columns in present.items()}},
    )


def _allowed_metrics() -> set[tuple[str, str]]:
    return {(plugin, name) for plugin, names in METRIC_ALLOWLIST.items() for name in names}


def _job_ids(rows: Sequence[dict]) -> list[int]:
    return [int(row["source_id"]) for row in rows]


def _attempt_from_job(row: dict, metrics: dict[str, Decimal]) -> NormalizedAttempt:
    """Galaxy's own record of one execution, before provider evidence arrives."""
    start = metrics.get("core.start_epoch")
    end = metrics.get("core.end_epoch")
    return NormalizedAttempt(
        job_source_id=row["source_id"],
        source_attempt_id="galaxy-0",
        runner=row["runner"] or "unassigned",
        outcome=row["state"],
        external_id=row["external_id"],
        exit_code=row["exit_code"],
        tool_started_at=datetime.fromtimestamp(float(start), UTC) if start is not None else None,
        tool_finished_at=datetime.fromtimestamp(float(end), UTC) if end is not None else None,
        correlation="galaxy_record",
        facts={"source": SOURCE_NAME},
    )


def _normalize_jobs(
    rows: Sequence[dict],
    metrics: dict[str, list[tuple[str, str, Decimal]]],
    events: dict[str, list[tuple[str, datetime]]],
) -> list[NormalizedJob]:
    jobs: list[NormalizedJob] = []
    for row in rows:
        source_id = row["source_id"]
        job_metrics = metrics.get(source_id, [])
        flat = {f"{plugin}.{name}": value for plugin, name, value in job_metrics}
        hints = {
            key: str(flat[key])
            for key in ("core.galaxy_slots", "core.galaxy_memory_mb", "core.runtime_seconds")
            if key in flat
        }
        jobs.append(
            NormalizedJob(
                source_id=source_id,
                owner_source_id=row["owner_source_id"] or "unattributed",
                tool_id=row["tool_id"],
                tool_version=row["tool_version"],
                state=row["state"],
                exit_code=row["exit_code"],
                runner=row["runner"],
                destination=row["destination"],
                handler=row["handler"],
                created_at=_utc(row["created_at"]),
                updated_at=_utc(row["updated_at"]),
                copied_from_source_id=row["copied_from_source_id"],
                resource_hints=hints,
                metrics=tuple(
                    NormalizedMetric(plugin, name, value) for plugin, name, value in job_metrics
                ),
                state_events=tuple(
                    NormalizedStateEvent(state, occurred) for state, occurred in events.get(source_id, [])
                ),
                attempts=(_attempt_from_job(row, flat),),
            )
        )
    return jobs


def _fetch_details(
    connection: Connection, ids: Sequence[int]
) -> tuple[
    dict[str, list[tuple[str, str, Decimal]]],
    dict[str, list[tuple[str, datetime]]],
]:
    metrics: dict[str, list[tuple[str, str, Decimal]]] = {}
    events: dict[str, list[tuple[str, datetime]]] = {}
    if not ids:
        return metrics, events
    allowed = _allowed_metrics()
    plugins = sorted({plugin for plugin, _ in allowed})
    names = sorted({name for _, name in allowed})
    for row in connection.execute(
        METRICS, {"ids": list(ids), "plugins": plugins, "names": names}
    ).mappings():
        if (row["plugin"], row["metric_name"]) not in allowed:
            continue
        metrics.setdefault(row["job_source_id"], []).append(
            (row["plugin"], row["metric_name"], Decimal(str(row["metric_value"])))
        )
    for row in connection.execute(STATE_EVENTS, {"ids": list(ids)}).mappings():
        events.setdefault(row["job_source_id"], []).append((row["state"], _utc(row["occurred_at"])))
    return metrics, events


def _fetch_owners(connection: Connection, owner_ids: Iterable[str]) -> list[NormalizedOwner]:
    numeric = sorted({int(value) for value in owner_ids if value and value.isdigit()})
    if not numeric:
        return []
    return [
        NormalizedOwner(source_id=row["source_id"], label=row["username"] or f"user {row['source_id']}")
        for row in connection.execute(OWNERS, {"ids": numeric}).mappings()
    ]


def _fetch_invocations(
    connection: Connection, *, root_ids: list[int] | None
) -> list[NormalizedInvocation]:
    tree = list(connection.execute(INVOCATION_TREE, {"root_ids": root_ids}).mappings())
    if not tree:
        return []
    memberships: dict[str, list[NormalizedMembership]] = {}
    for row in connection.execute(INVOCATION_MEMBERSHIP, {"root_ids": root_ids}).mappings():
        # A job whose owner differs from the invocation owner is preserved as a
        # fact but never used to widen a viewer's authorized cohort.
        memberships.setdefault(row["invocation_source_id"], []).append(
            NormalizedMembership(
                job_source_id=row["job_source_id"],
                step_key=f"{row['workflow_step_id']}:{row['step_id']}",
                relationship=row["relationship"],
            )
        )
    settled = {
        row["invocation_source_id"]: row["unscheduled_steps"] == 0
        for row in connection.execute(
            SETTLED_INVOCATION_STEPS,
            {"ids": [int(row["invocation_source_id"]) for row in tree]},
        ).mappings()
    }
    return [
        NormalizedInvocation(
            source_id=row["invocation_source_id"],
            owner_source_id=row["invocation_owner_source_id"] or "unattributed",
            workflow_name=row["workflow_name"] or f"workflow {row['workflow_id']}",
            state=row["state"],
            created_at=_utc(row["created_at"]),
            workflow_id=row["workflow_id"],
            workflow_family_id=row["workflow_family_id"],
            workflow_version=row["workflow_id"],
            parent_source_id=row["parent_source_id"],
            membership_settled=settled.get(row["invocation_source_id"], False),
            memberships=tuple(memberships.get(row["invocation_source_id"], ())),
        )
        for row in tree
    ]


class GalaxyDatabaseAdapter:
    """Resumable extraction: backfill by job ID, then incremental plus reconciliation.

    Job update timestamps are not a complete change feed for metrics or
    workflow association tables, so every incremental pass also reconciles
    nonterminal jobs, recently terminal jobs and active invocations, and sweeps
    older records in bounded batches.
    """

    source_name = SOURCE_NAME

    def __init__(
        self,
        engine: Engine,
        *,
        batch_size: int = 500,
        overlap_seconds: int = 300,
        recent_terminal_window: timedelta = timedelta(hours=6),
        statement_timeout: str = "30s",
        now: datetime | None = None,
    ) -> None:
        if not 1 <= batch_size <= 5000:
            raise ValueError("batch_size must be between 1 and 5000")
        self._engine = engine
        self._batch_size = batch_size
        self._overlap = timedelta(seconds=overlap_seconds)
        self._recent_window = recent_terminal_window
        self._statement_timeout = statement_timeout
        self._now = now

    def _clock(self) -> datetime:
        return self._now or datetime.now(UTC)

    def collect(self, cursor: dict) -> ObservationBatch:
        started = self._clock()
        with self._engine.connect() as connection:
            _read_only(connection, self._statement_timeout)
            state = dict(cursor)
            phase = state.get("phase", "backfill")
            if phase == "backfill":
                return self._backfill(connection, state, started)
            return self._incremental(connection, state, started)

    def _backfill(self, connection: Connection, state: dict, started: datetime) -> ObservationBatch:
        ceiling = state.get("backfill_ceiling")
        if ceiling is None:
            ceiling = int(connection.execute(MAX_JOB_ID).scalar() or 0)
            state["backfill_ceiling"] = ceiling
        id_after = int(state.get("backfill_id_after", 0))
        rows = list(
            connection.execute(
                BACKFILL_PAGE,
                {"id_after": id_after, "id_ceiling": ceiling, "batch_size": self._batch_size},
            ).mappings()
        )
        batch = self._build(connection, rows, started, source_phase="backfill")
        if rows:
            state["backfill_id_after"] = max(_job_ids(rows))
            exhausted = len(rows) < self._batch_size
        else:
            exhausted = True
        if exhausted:
            state["phase"] = "incremental"
            state["updated_after"] = state.get("updated_after") or "1970-01-01T00:00:00+00:00"
            state["id_after"] = 0
        return ObservationBatch(
            source=self.source_name,
            observed_at=started,
            owners=batch.owners,
            jobs=batch.jobs,
            invocations=batch.invocations,
            cursor=state,
            metrics={"phase": "backfill", "rows": len(rows), "ceiling": ceiling},
            exhausted=exhausted,
        )

    def _incremental(
        self, connection: Connection, state: dict, started: datetime
    ) -> ObservationBatch:
        watermark = datetime.fromisoformat(state.get("updated_after", "1970-01-01T00:00:00+00:00"))
        replay_from = watermark - self._overlap
        rows = list(
            connection.execute(
                INCREMENTAL_PAGE,
                {
                    "updated_after": replay_from.replace(tzinfo=None),
                    # The replay overlap intentionally revisits recent rows, so
                    # the tie-breaking ID only applies without an overlap.
                    "id_after": int(state.get("id_after", 0)) if not self._overlap else 0,
                    "batch_size": self._batch_size,
                },
            ).mappings()
        )
        exhausted = len(rows) < self._batch_size
        reconciled: list[dict] = []
        root_ids: list[int] | None = None
        if exhausted:
            reconciled, root_ids, state = self._reconcile(connection, state, started)
        merged = {row["source_id"]: row for row in [*rows, *reconciled]}
        ordered = [merged[key] for key in sorted(merged, key=int)]
        batch = self._build(connection, ordered, started, source_phase="incremental", root_ids=root_ids)
        if rows:
            last = rows[-1]
            state["updated_after"] = _utc(last["updated_at"]).isoformat()
            state["id_after"] = int(last["source_id"])
        return ObservationBatch(
            source=self.source_name,
            observed_at=started,
            owners=batch.owners,
            jobs=batch.jobs,
            invocations=batch.invocations,
            cursor=state,
            metrics={
                "phase": "incremental",
                "rows": len(rows),
                "reconciled": len(reconciled),
                "replay_overlap_seconds": int(self._overlap.total_seconds()),
            },
            exhausted=exhausted,
        )

    def _reconcile(
        self, connection: Connection, state: dict, started: datetime
    ) -> tuple[list[dict], list[int], dict]:
        ids: set[int] = set()
        ids.update(
            connection.scalars(
                NONTERMINAL_IDS,
                {"terminal_states": list(TERMINAL_STATES), "batch_size": self._batch_size},
            )
        )
        ids.update(
            connection.scalars(
                RECENTLY_TERMINAL_IDS,
                {
                    "terminal_states": list(TERMINAL_STATES),
                    "since": (started - self._recent_window).replace(tzinfo=None),
                    "batch_size": self._batch_size,
                },
            )
        )
        sweep_after = int(state.get("sweep_id_after", 0))
        sweep = list(connection.scalars(SWEEP_IDS, {"id_after": sweep_after, "batch_size": self._batch_size}))
        ids.update(sweep)
        # The sweep wraps once it reaches the end, so late metrics or
        # memberships on older jobs are eventually revisited.
        state["sweep_id_after"] = max(sweep) if len(sweep) == self._batch_size else 0
        roots = set(
            connection.scalars(
                ACTIVE_INVOCATION_ROOTS,
                {
                    "since": (started - self._recent_window).replace(tzinfo=None),
                    "batch_size": self._batch_size,
                },
            )
        )
        if ids:
            roots.update(connection.scalars(INVOCATION_ROOTS_FOR_JOBS, {"ids": sorted(ids)}))
        rows = list(connection.execute(JOBS_BY_ID, {"ids": sorted(ids)}).mappings()) if ids else []
        return rows, sorted(roots), state

    def _build(
        self,
        connection: Connection,
        rows: Sequence[dict],
        started: datetime,
        *,
        source_phase: str,
        root_ids: list[int] | None = None,
    ) -> ObservationBatch:
        ids = _job_ids(rows)
        metrics, events = _fetch_details(connection, ids)
        owners = _fetch_owners(connection, {row["owner_source_id"] for row in rows})
        jobs = _normalize_jobs(rows, metrics, events)
        if root_ids is None and ids:
            root_ids = sorted(set(connection.scalars(INVOCATION_ROOTS_FOR_JOBS, {"ids": ids})))
        invocations = _fetch_invocations(connection, root_ids=root_ids or None) if root_ids else []
        invocation_owner_ids = {invocation.owner_source_id for invocation in invocations}
        known = {owner.source_id for owner in owners}
        owners = [*owners, *_fetch_owners(connection, invocation_owner_ids - known)]
        return ObservationBatch(
            source=self.source_name,
            observed_at=started,
            owners=tuple(owners),
            jobs=tuple(jobs),
            invocations=tuple(invocations),
            metrics={"phase": source_phase},
        )


def read_job_batch(
    engine: Engine, *, updated_after: datetime, id_after: int, batch_size: int = 1000
) -> list[dict]:
    """Single incremental page, retained for focused schema checks and tests."""
    if not 1 <= batch_size <= 5000:
        raise ValueError("batch_size must be between 1 and 5000")
    with engine.connect() as connection:
        _read_only(connection, "30s")
        return [
            dict(row)
            for row in connection.execute(
                INCREMENTAL_PAGE,
                {"updated_after": updated_after, "id_after": id_after, "batch_size": batch_size},
            ).mappings()
        ]
