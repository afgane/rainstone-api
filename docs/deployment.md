# Deployment, identity and diagnostics

Rainstone installs alongside Galaxy. Installation, operation and acceptance
diagnostics all run inside the Galaxy VM: no remote kubeconfig, SSH access or
externally hosted monitoring is required, and none of them is a prerequisite for
support.

## Identity modes

The mode is configuration, never inferred from a URL prefix.

| Mode | Use | Behavior |
| --- | --- | --- |
| `anvil-workspace` | AnVIL deployments behind the platform's authenticated route | One fixed tenant and one fixed shared Galaxy account; client input cannot change scope |
| `trusted-proxy` | Standalone and multi-user deployments | Identity comes only from a proxy that strips client copies of the same headers; no fixture defaults |
| `development` | Local work and tests | Fixture identities; startup fails unless demo mode is enabled |

In `anvil-workspace` mode, startup fails when the shared account or tenant
identity is missing, so a fixture identity is never used silently. Any
`X-Rainstone-*` header on a request is rejected with 403 rather than honored,
`owner` filters cannot select another account, and the per-user view stays
unavailable. The packaged browser application only sends identity headers when
the served page reports development mode.

Workspace viewers may read that account's job reports and, when enabled, the
separately scoped infrastructure view. These are reporting capabilities, not
Galaxy administrator privileges. The UI states that attribution is to the shared
Galaxy account, not to individual workspace members.

Human-level attribution and hosted multi-user authentication remain separate
future work. This mode relies on the platform's external admission boundary; a
directly reachable development VM is not authenticated by it.

## Chart

`chart/` deploys the web and collector as separate workloads from the same
versioned image, so they restart independently and hold different credentials.

- The web process serves reports and holds no Galaxy source credential and no
  cloud observation permissions.
- The collector holds the read-only source DSN, uses in-cluster Kubernetes
  authentication, and reads cloud APIs with the deployment's own runtime
  identity. An operator kubeconfig is never mounted.
- Web, collector and initialization run under separate service accounts.
  Observation permissions and any Workload Identity binding belong to the
  collector; initialization may write only the source credential Secret; the web
  account is bound to nothing. One honest limitation: on a node whose metadata
  server is reachable from any pod, that node-wide credential is available
  regardless of service accounts, so restricting the metadata server remains a
  deployment prerequisite this chart cannot enforce.
- Rainstone's database is either an existing DSN Secret or the chart's own
  PostgreSQL deployment. Its volume and credential are annotated
  `helm.sh/resource-policy: keep`, so reporting history survives
  `helm uninstall` unless it is removed deliberately.
- Migrations run as an explicit one-shot Job on install and upgrade, before new
  application pods become ready. The Job also refreshes the catalog and records
  a self-check result.
- The service is ClusterIP and the ingress registers the Galaxy-derived path.
  The chart adds no NodePort, LoadBalancer, public address or extra listener.
  The optional NetworkPolicy restricts in-cluster callers only; it cannot prove
  that a request arrived through the platform's authenticated route.
- Collector RBAC is observation only: get, list and watch on pods, batch jobs
  and nodes. No log or exec access.

Boot supplies the instance slug, base path, source Secret, project and region,
and the baseline descriptor. Operator-facing settings stay small: enablement,
path, persistence and retention, optional price feed, and source overrides. No
one configures individual SKU prices.

## Initialization sequence

Install and upgrade run one ordered sequence, and every step is idempotent, so
an interrupted run is simply re-run:

1. wait for the Rainstone database;
2. `alembic upgrade head`;
3. `rainstone bootstrap` — provision or rotate the scoped reader, enroll this
   source database's identity, resolve the shared Galaxy account, seed the
   instance and baseline policy, and write the reader DSN into the source
   Secret;
4. import or refresh the price catalog;
5. record a self-check in the initialization context.

The chart runs this as a post-install and post-upgrade hook, because the
chart's own database does not exist before install. Application pods do not
depend on hook ordering: each waits on `rainstone wait-ready` in an init
container, so no pod serves against an unmigrated or unenrolled database, and a
rollout completes only after initialization does.

Supply `source.adminSecret` (a Secret holding an installation-time DSN) and
`source.sharedAccount`. The application never receives that credential: the
initialization account may write exactly one Secret, the scoped reader DSN that
the collector mounts. Running the command directly is still supported:

```console
rainstone bootstrap \
    --admin-database-url postgresql+psycopg://…@galaxy-postgres-rw:5432/galaxy \
    --shared-account <galaxy account> \
    --write-dsn /secrets/dsn
```

Use Galaxy's primary database service with that dedicated read-only role. On a
single-instance deployment the replica-only `-ro` service can have no endpoints,
so a service named "read-only" is not sufficient; derive the hostname from the
chart or deployment configuration.

### Source identity

Identity is an identifier enrolled inside the source database, in a `rainstone`
schema the reader may select from. It is deliberately not a schema hash and not
a release name: column-level grants make an administrator and the restricted
reader see different columns of the same database, while two unrelated
databases can share a schema exactly.

That identifier survives upgrades and restores of the source database. A
*different* database is refused with an explicit message until an operator
enrolls it with `rainstone bootstrap --replace-source`, so a replacement can
never inherit an instance's history and job IDs by accident.

### Readiness and liveness

Readiness reflects a compatible schema and a resolved fixed scope: `/api/ready`
returns 503 with reasons until migrations match this release and the configured
shared account resolves to exactly one owner. Liveness stays cheap and
independent — `/api/health` for the web process, and a loop heartbeat
(`rainstone heartbeat`) for the collector. A denied cloud API or an unreachable
source degrades coverage and backs off; it does not restart collection from
healthy sources.

## Diagnostics

Checks run in the context that holds the credentials for them. The collector and
initialization probe the source database, Kubernetes access and the cloud
operations each enabled adapter actually calls, then record a sanitized,
timestamped report. The web process runs local checks — database, migration
state, instance identity, account resolution, baseline, catalog, collection lag
and gaps — and reports the recorded findings alongside them.

Recorded findings carry their own timestamp and age. A report older than fifteen
minutes is marked stale and a stale success is downgraded to a warning, so an
absent or lagging collector can never read as a current successful check.

Cloud probes distinguish outcomes that used to look alike: `denied` (the
deployment identity lacks the permission), `unavailable` (the API could not be
reached) and `ok` — a missing resource is a valid answer from an API that
answered, not a pass for an API that refused. Optional enrichment that is denied
degrades coverage with a visible gap rather than failing collection.

Findings name capability gaps without exposing DSNs, credentials, tokens, raw
job parameters or arbitrary logs, so the report can be downloaded through the
normal authenticated route from the Status view. Read-only checks run
automatically; submitting synthetic jobs is an opt-in development action.

## Current limitations

- Price coverage is `us-central1` only, from a pinned 2026-09-19 snapshot. The
  maintained catalog feed and publisher are not yet operated, so this release
  cannot claim unattended current-price reporting. Enabling a feed requires
  `catalog.trustedKeys`; the chart refuses to render a feed URL without one.
- The AnVIL dev pilot, live Leo-route validation, restart and upgrade exercises,
  and completed-job visibility measurement are not yet done.
- Billing reconciliation, Spot and preemption completeness, and hosted transport
  remain separately scoped.
