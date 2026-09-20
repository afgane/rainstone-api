"""Bounded, allowlisted Galaxy database extraction.

The adapter deliberately returns logical job facts only. Execution-resource evidence
is joined later by runner adapters, so Galaxy destination parameters are never copied
wholesale into Rainstone.
"""

from datetime import datetime

from sqlalchemy import Engine, text

QUERY = text("""
    SELECT j.id::text AS source_id,
           j.user_id::text AS owner_source_id,
           j.tool_id,
           j.tool_version,
           j.state,
           j.job_runner_name AS runner,
           j.destination_id AS destination,
           j.create_time AS created_at,
           j.update_time AS updated_at
      FROM job j
     WHERE (j.update_time, j.id) > (:updated_after, :id_after)
     ORDER BY j.update_time, j.id
     LIMIT :batch_size
""")


def read_job_batch(
    engine: Engine, *, updated_after: datetime, id_after: int, batch_size: int = 1000
) -> list[dict]:
    if not 1 <= batch_size <= 5000:
        raise ValueError("batch_size must be between 1 and 5000")
    with engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
        return [
            dict(row)
            for row in connection.execute(
                QUERY, {"updated_after": updated_after, "id_after": id_after, "batch_size": batch_size}
            ).mappings()
        ]
