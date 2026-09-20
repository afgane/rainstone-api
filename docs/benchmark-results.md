# Rainstone Phase 2A reporting benchmark

- Generated jobs: 100,000 (transaction rolled back)
- Host: Linux-6.12.76-linuxkit-aarch64-with-glibc2.41 · aarch64 · Python 3.12.11
- PostgreSQL URL host: postgres
- Corpus generation: 46.48 s
- Query: runner=gcp_batch, search=benchmark/tool/3 (10,000 matching jobs), amount sort
- Eight iterations; first is cold with respect to application objects, later runs are warm.

| Request | Cold | Warm p50 | Warm p95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| summary | 1344.5 ms | 1309.9 ms | 1328.2 ms | 1344.5 ms |
| first_page | 1191.3 ms | 1193.6 ms | 1203.6 ms | 1203.6 ms |
| deep_page | 1210.3 ms | 1192.9 ms | 1215.4 ms | 1215.4 ms |

Target: ordinary interactive requests below 2,000 ms.

## Representative database plan

The leading-wildcard shared search uses a sequential scan at this scale; total request time above also includes authorization, cost-line loading, aggregation, stable sorting, and serialization.

```text
Aggregate  (cost=497.13..497.14 rows=1 width=8) (actual time=229.296..229.297 rows=1 loops=1)
  Buffers: shared hit=102150
  ->  Nested Loop  (cost=9.44..497.12 rows=1 width=0) (actual time=2.038..228.878 rows=10000 loops=1)
        Join Filter: ((job.owner_id = owner.id) AND (((job.source_id)::text ~~* '%benchmark/tool/3%'::text) OR ((job.tool_id)::text ~~* '%benchmark/tool/3%'::text) OR ((COALESCE(job.tool_version, ''::character varying))::text ~~* '%benchmark/tool/3%'::text) OR ((owner.label)::text ~~* '%benchmark/tool/3%'::text)))
        Rows Removed by Join Filter: 290018
        Buffers: shared hit=102150
        ->  Bitmap Heap Scan on job  (cost=9.44..484.20 rows=1 width=1168) (actual time=2.003..19.425 rows=100006 loops=1)
              Recheck Cond: (tenant_id = 'fa96280f-b720-5125-adee-f68e69818402'::uuid)
              Filter: ((runner)::text = 'gcp_batch'::text)
              Rows Removed by Filter: 9
              Heap Blocks: exact=2042
              Buffers: shared hit=2144
              ->  Bitmap Index Scan on ix_job_tool_version  (cost=0.00..9.43 rows=153 width=0) (actual time=1.819..1.819 rows=100015 loops=1)
                    Index Cond: (tenant_id = 'fa96280f-b720-5125-adee-f68e69818402'::uuid)
                    Buffers: shared hit=102
        ->  Seq Scan on owner  (cost=0.00..10.90 rows=90 width=434) (actual time=0.000..0.000 rows=3 loops=100006)
              Buffers: shared hit=100006
Planning:
  Buffers: shared hit=3
Planning Time: 0.135 ms
Execution Time: 229.337 ms
```
