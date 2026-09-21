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

## Bootstrap

`rainstone bootstrap` runs once per install or upgrade with installation-time
database privileges that the application never receives:

```console
rainstone bootstrap \
    --admin-database-url postgresql+psycopg://…@galaxy-postgres-rw:5432/galaxy \
    --shared-account <galaxy account> \
    --write-dsn /secrets/dsn
```

It creates or rotates a dedicated reader role with column-level grants over the
allowlisted tables, writes the scoped DSN for the chart to store, resolves the
shared Galaxy account to its source owner ID, seeds this instance's identity,
binds that identity to the source database, and records the baseline accounting
policy.

Use Galaxy's primary database service with that dedicated read-only role. On a
single-instance deployment the replica-only `-ro` service can have no endpoints,
so a service named "read-only" is not sufficient; derive the hostname from the
chart or deployment configuration.

The binding matters on reuse: if the source database is replaced, the collector
refuses to inherit the previous instance identity and asks for a new one, rather
than silently mixing two sources' history.

## Diagnostics

`rainstone doctor --json` and `/api/status` report the same read-only checks:
database connectivity and migration state, instance identity and source
binding, shared-account resolution, source schema compatibility, Kubernetes list
access, supported cloud reads, baseline resolution, catalog availability and
coverage, and per-source collection lag, cursors and recorded gaps.

Findings name capability gaps without exposing DSNs, credentials, tokens, raw
job parameters or arbitrary logs, so the report can be downloaded through the
normal authenticated route from the Status view. The collector's liveness probe
runs the same checks. Read-only checks run automatically; submitting synthetic
jobs is an opt-in development action.

## Current limitations

- Price coverage is `us-central1` only, from a pinned 2026-09-19 snapshot. The
  maintained catalog feed and publisher are not yet operated, so this release
  cannot claim unattended current-price reporting.
- The AnVIL dev pilot, live Leo-route validation, restart and upgrade exercises,
  and completed-job visibility measurement are not yet done.
- Billing reconciliation, Spot and preemption completeness, and hosted transport
  remain separately scoped.
