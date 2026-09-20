# Rainstone v2

Rainstone reports estimated compute cost for work already performed by Galaxy.
Phase 1 is a local-first vertical slice: it persists normalized source facts in
PostgreSQL, calculates additional-spend and allocated-cost views, enforces
owner/tenant authorization in API queries, and serves a compact Vue dashboard.

The bundled demonstration is sanitized from the 2026-09-19 Phase 0 observations.
It includes the three recorded dedicated Batch calculations, a cross-runner
retry, a mixed Kubernetes/Batch invocation, a nested collection invocation,
known-zero existing capacity, unknown execution topology, two owners, and a
separate baseline-VM interval.
No operational endpoint, credential, raw log, or user dataset is included.

## Run locally

Docker with Compose is the only required host dependency.

```console
make dev
```

Open <http://localhost:5173>. The first run builds the toolchains, waits for
PostgreSQL, applies the Alembic migration, and idempotently ingests the fixture.
`make dev-down` retains data; `make dev-reset` explicitly removes only the
Compose project's development volume and reseeds it.

```console
make test       # backend unit/integration, frontend unit, type and lint checks
make e2e        # release image and Playwright browser test
make build      # production image with Vue embedded in FastAPI
```

## Commands

The release image exposes one CLI and one server process:

```console
rainstone ingest-fixtures --path fixtures/phase1.json
rainstone validate-catalog --path catalog/gcp-us-central1-2026-09-19.json
uvicorn rainstone.main:app --host 0.0.0.0 --port 8000
```

The fixture command is replay-safe. It upserts source facts using stable source
identities, records an ingestion cursor, and reuses a calculation revision when
its source-fact and price digest is unchanged.

## Identity contract

Development mode provides explicit fixture identities through
`X-Rainstone-Tenant`, `X-Rainstone-User`, and optional
`X-Rainstone-Admin: true` headers. It is rejected unless demo mode is enabled.
Trusted-proxy mode requires the same values from a proxy that strips client
headers before forwarding; real AnVIL proxy validation remains Phase 2 work.
Every jobs, totals, search, and invocation query applies the resolved tenant and
owner scope in SQL.

## Cost semantics

- `additional`: known zero for policy-verified baseline capacity; dedicated VM
  lifecycle cost when priced; unknown remains null.
- `allocated`: dedicated VM cost, while baseline allocation remains unavailable
  until a valid T2D component/allocation policy exists.
- Baseline infrastructure cost is shown separately and is never added to job
  allocations.

The bundled price snapshot was observed on 2026-09-19 from Google's official
general-purpose VM pricing page. It has no Catalog API effective timestamps, so
calculations are marked approximate. Disks, network, discounts, credits, and
taxes are excluded.

The adopted design-system revisions are recorded in
[`docs/design-reference.md`](docs/design-reference.md).
