# Rainstone v2

Rainstone reports estimated compute cost for work already performed by Galaxy.
One version-pinned query contract drives overview, job, tool, workflow, daily,
user, infrastructure, detail, status, and CSV responses. The Vue application
restores filters and pagination from its URL and works at the root or behind a
stripped proxy prefix.

Phase 2B adds unattended collection and the deployment contract: a read-only
Galaxy database adapter, Kubernetes and GCP Batch/Compute/Logging observation,
chargeable resource lifetimes shared by retry attempts, a versioned price
catalog consumer, the fixed `anvil-workspace` identity mode, an independently
versioned Helm chart, on-VM bootstrap, and read-only self-checks. See
[`docs/collection.md`](docs/collection.md) and
[`docs/deployment.md`](docs/deployment.md). The AnVIL dev pilot, live route
validation, and the maintained catalog feed are not yet done; the status report
names those gaps rather than implying coverage.

The bundled demonstration starts with sanitized 2026-09-19 Phase 0
observations. It includes the three recorded dedicated Batch calculations, a
cross-runner retry, a mixed Kubernetes/Batch invocation, a nested collection
invocation, known-zero existing capacity, unknown execution topology, two
owners, and a separate baseline-VM interval. Clearly marked synthetic cases add
a DST boundary, in-progress and awaiting-calculation work, output reuse, and a
same-VM provider retry whose one lifetime is charged once across two attempts.
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
make benchmark  # rolled-back 100,000-job reporting benchmark
```

## Commands

The release image exposes one CLI, a web process, and a collector process:

```console
rainstone ingest-fixtures --path fixtures/phase1.json
rainstone collect [--cycles N]
rainstone bootstrap --admin-database-url … --shared-account … --write-dsn …
rainstone wait-ready [--timeout 600]
rainstone heartbeat [--max-age 300]
rainstone doctor
rainstone catalog validate|import|refresh|coverage [--path …]
uvicorn rainstone.main:app --host 0.0.0.0 --port 8000
```

`make collect`, `make doctor`, `make catalog` and `make chart-lint` run these
against the development environment and lint the packaged chart.

The fixture command is replay-safe. It upserts source facts using stable source
identities, records an ingestion cursor, and reuses a calculation revision when
its source-fact and price digest is unchanged.

## Identity contract

Three explicit modes, selected by configuration rather than inferred from a URL
prefix. `development` provides fixture identities through `X-Rainstone-Tenant`,
`X-Rainstone-User` and optional `X-Rainstone-Admin: true`, and is rejected
unless demo mode is enabled. `trusted-proxy` requires those values from a proxy
that strips client copies first. `anvil-workspace` reports one fixed tenant and
shared Galaxy account and rejects any client identity header; the packaged
browser application sends no identity headers outside development mode. Every
detail, table, total, search, invocation, user, infrastructure, status and
export query applies the resolved tenant and owner scope on the backend.
[`docs/deployment.md`](docs/deployment.md) has the deployment contract.

## Cost semantics

- `additional`: known zero for policy-verified baseline capacity; dedicated VM
  lifecycle cost when priced; unknown remains null.
- `allocated`: dedicated VM cost, while baseline allocation remains unavailable
  until a valid T2D component/allocation policy exists.
- Baseline infrastructure cost is shown separately and is never added to job
  allocations.
- The default reporting mode accrues cost within half-open from/to intervals.
  Minimum-charge uplift is distributed proportionally across the observed
  positive-duration lifetime; untimed costs remain unattributed.
- Partial amounts are known subtotals. Unknown amounts stay blank in CSV and are
  distinct from exact zero. Unsupported currencies are rejected.
- A separately labeled completed-job cohort supplies historical tool statistics;
  its percentiles are never added across workflow steps.

- A resource lifetime is priced once per basis and component, with the provider
  minimum applied once to that lifetime. Retries that reuse one VM share that
  charge instead of repeating it; work shared by two Galaxy jobs becomes
  unavailable pending an allocation policy.

Catalog artifacts fetched from a feed are untrusted until an Ed25519 signature
verifies against a release-pinned key; a feed cannot be configured without one.

The bundled price snapshot was observed on 2026-09-19 from Google's official
general-purpose VM pricing page, covering `us-central1` only. It has no Catalog
API effective timestamps, so calculations are marked approximate. Shapes and
regions outside the catalog, including the observed `us-east4` N2 shapes, stay
visibly unpriced. Disks, network, discounts, credits, and taxes are excluded.

The adopted design-system revisions are recorded in
[`docs/design-reference.md`](docs/design-reference.md).
The shared filter and accounting contract is documented in
[`docs/reporting-contract.md`](docs/reporting-contract.md), collection and
resource accounting in [`docs/collection.md`](docs/collection.md), and the
measured
100,000-job results and query plan are in
[`docs/benchmark-results.md`](docs/benchmark-results.md).
