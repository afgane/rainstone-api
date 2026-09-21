# Collection and resource accounting

`rainstone collect` runs independently of `serve`, from the same image, with one
active collector per enrolled Galaxy instance. It holds a PostgreSQL advisory
lease, so a second process refuses to collect rather than double-counting. Web
availability does not depend on it.

One writer per instance also keeps writes contention-free: advancing the
report-generation marker takes a per-tenant row lock, so two concurrent
ingestions of the same tenant would serialize.

Each cycle collects one bounded batch per due source and commits that batch,
its cursor and the recalculated costs together, so reports never see
observations without a matching calculation revision and an interrupted cycle
is simply re-collected. A failure records a visible source status with an error
type, backs off with jitter, and leaves the cursor alone. An unchanged set of
source facts reuses the existing revision instead of creating another.

## Galaxy database extraction

The adapter discovers schema capabilities first and refuses to run against an
unsupported schema. Every statement names its columns, runs read-only with a
statement timeout, and reads logical job facts only: no command lines,
stdout/stderr, tracebacks, credentials, dataset contents or the pickled
`destination_params` blob. Resource hints come from allowlisted numeric metrics.

Collection has three phases:

| Phase | Behavior |
| --- | --- |
| Backfill | Pages by stable job ID up to a recorded upper bound |
| Incremental | Pages by `(update_time, id)` with a replay overlap |
| Reconciliation | Nonterminal jobs, recently terminal jobs, active invocation trees, and a wrapping sweep of older jobs in bounded batches |

Reconciliation exists because job update timestamps are not a complete change
feed: a metric or a collection membership can appear without the parent job's
timestamp changing. The sweep wraps when it reaches the end, so older records
are eventually revisited.

Workflow attribution uses the recursive query proven on the AnVIL dev instance:
root invocations expand through subworkflow associations, and each step
contributes direct jobs plus implicit-collection expansions. Invocation
ownership comes from the invocation's history, because this schema has no
`workflow_invocation.user_id`. Job ownership is enforced separately, so a
mismatched membership never widens a viewer's authorized cohort. Invocations
record whether their step scheduling has settled; membership is revisited until
it has.

## Kubernetes observation

Pods are observed by list plus watch, tracking the list resource version,
relisting after an expired version, and recording an **unrecoverable** gap for
that window, because pods deleted while disconnected cannot be recovered.

A pod's occupancy of node capacity is one lifetime, keyed by pod UID, and the
node keeps its own identity in `resource_uid`. Reserved time starts when the pod
is scheduled, so image pulls and setup consume capacity while unscheduled queue
time does not. Tool time is recorded separately, each container run is its own
segment, and restarts do not merge into one interval. Admitted requests include
init containers. A node without a provider ID gives no verified VM identity, and
that qualification travels with the observation.

## GCP Batch, Compute and Logging observation

Galaxy stores a short Batch job name, so every read combines it with the
configured project and location and retains the full resolved name and UID. A
name prefix never implies an environment or an outcome; correlation uses the
provider's Galaxy job label.

Provider retries appear in **task** status events, not in the top-level job
status. The collector splits task events into per-attempt records with task
index, attempt ordinal, exit code and provider outcome, so a successful final
state never hides an earlier failed attempt. Galaxy state and exit code stay
separate from provider state: a `SUCCEEDED` Batch task never overwrites a Galaxy
error.

VM identity parsed from task-event text is labeled as a text-parsed correlation
hint. Live Compute instance timestamps are preferred; Logging insert and delete
operation markers recover a lifecycle window after a VM is deleted, with their
operation IDs and an explicit uncertainty note. When only task events are
available, the lifetime is marked as missing provisioning overhead. Every API
listing is paginated.

## Resource lifetimes and attempts

A **resource lifetime** is one chargeable provider resource, keyed by project,
zone and numeric instance ID for a GCE VM. Attempts associate with it. CI job
336 proves two attempts can use one VM, so:

- the lifetime is priced once per basis and component;
- the provider minimum applies once to the lifetime, with its uplift
  distributed proportionally across observed positive-duration windows, never
  once per attempt or per daily bucket;
- the charge is shown as shared across those attempts, and no attempt row
  repeats the full amount;
- two different VMs used by retries still produce two lifetime charges.

If more than one Galaxy job shares a lifetime, the charge becomes unavailable
with a reason, pending an explicit allocation policy and occupancy coverage.
Verified baseline capacity keeps its known-zero additional spend independently
of sharing.

Several observations of one resource are merged by evidence precedence:
provider-billable, then live Compute timestamps, then audit lifecycle markers,
then Kubernetes occupancy, then task events, then configured baseline
occupancy. Lower-precedence evidence never overwrites better evidence.

## Price coverage

The bundled catalog covers the `us-central1` shapes captured in Phase 0. The CI
corpus uses nine N2 shapes in `us-east4`, which this release cannot price: those
lifetimes stay visibly **unpriced** rather than borrowing another region's or a
different shape's rate. `rainstone catalog coverage` lists what can be priced,
and `/api/catalog` reports the same through the UI.

Catalog artifacts are content-addressed and immutable: a published catalog ID
cannot be replaced with different content. A refresh validates the artifact
before trusting it, imports prices and activates the version in one transaction,
and on failure keeps the last known good catalog. Without a configured feed the
active catalog is a pinned historical snapshot, and the status report says so.
