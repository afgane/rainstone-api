# Rainstone reporting benchmark

- Generated jobs: 100,000 (transaction rolled back)
- Host: Linux-7.0.12-linuxkit-aarch64-with-glibc2.41 · aarch64 · Python 3.12.11
- PostgreSQL URL host: postgres
- Corpus generation: 63.18 s
- Query: runner=gcp_batch, search=benchmark/tool/3 (10,000 matching jobs), amount sort
- Eight iterations; first is cold with respect to application objects, later runs are warm.
- The corpus is loaded with the report-generation triggers suspended inside the rolled-back transaction: this measures report latency, not ingestion. Those triggers add one small upsert per write statement, so bulk backfill favors batched writes.

| Request | Cold | Warm p50 | Warm p95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| summary | 1429.6 ms | 1360.0 ms | 1412.9 ms | 1429.6 ms |
| first_page | 1359.6 ms | 1363.6 ms | 1398.8 ms | 1398.8 ms |
| deep_page | 1391.7 ms | 1389.4 ms | 1426.7 ms | 1426.7 ms |

Target: ordinary interactive requests below 2,000 ms.

## Representative database plan

The leading-wildcard shared search uses a sequential scan at this scale; total request time above also includes authorization, cost-line loading, aggregation, stable sorting, and serialization.

```text
Aggregate  (cost=28.60..28.61 rows=1 width=8) (actual time=227.019..227.020 rows=1 loops=1)
  Buffers: shared hit=243141
  ->  Nested Loop  (cost=0.54..28.59 rows=1 width=0) (actual time=70.242..226.564 rows=10000 loops=1)
        Buffers: shared hit=243141
        ->  Index Scan using ix_job_tool_version on job  (cost=0.39..20.41 rows=1 width=1168) (actual time=0.054..43.497 rows=100008 loops=1)
              Index Cond: (tenant_id = 'fa96280f-b720-5125-adee-f68e69818402'::uuid)
              Filter: ((runner)::text = 'gcp_batch'::text)
              Rows Removed by Filter: 9
              Buffers: shared hit=43125
        ->  Index Scan using owner_pkey on owner  (cost=0.14..8.17 rows=1 width=434) (actual time=0.002..0.002 rows=0 loops=100008)
              Index Cond: (id = job.owner_id)
              Filter: (((job.source_id)::text ~~* '%benchmark/tool/3%'::text) OR ((job.tool_id)::text ~~* '%benchmark/tool/3%'::text) OR ((COALESCE(job.tool_version, ''::character varying))::text ~~* '%benchmark/tool/3%'::text) OR ((label)::text ~~* '%benchmark/tool/3%'::text))
              Rows Removed by Filter: 1
              Buffers: shared hit=200016
Planning:
  Buffers: shared hit=1 read=2
Planning Time: 0.128 ms
Execution Time: 227.048 ms
```
