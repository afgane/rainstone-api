# Deployment, identity and diagnostics

Rainstone installs alongside Galaxy and fits the deployment it finds.
Installation, operation and acceptance diagnostics all run inside the Galaxy VM:
no remote kubeconfig, SSH access, developer cloud login or externally hosted
monitoring is required, and none of them is a prerequisite for support.

Installing Rainstone adds Rainstone's own workloads, storage, route and
observation permissions. It does not change Galaxy's runner configuration, VM
identity, IAM bindings, metadata access, cluster topology or Leonardo, and
Leonardo remains the only production entry point.

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

## Image

The release image is published to Docker Hub as `afgane/rainstone`, tagged with
the chart's `appVersion`. Cluster nodes are `linux/amd64`, so build for that
platform explicitly when the build host is an Apple Silicon machine:

```console
docker buildx build --platform linux/amd64 --target runtime \
    -t afgane/rainstone:2.0.0b1 --push .
```

`make build` produces the same image locally as `rainstone:local` for the host's
own architecture; it is for local checks, not for the cluster.

## Discovery

`rainstone discover` reads what the cluster and the host already record and
prints one resolved description, with Helm values for the fields it resolved:

```console
rainstone discover --values /tmp/rainstone-values.yaml
```

It reads the Kubernetes API in-cluster, or through a local `kubectl` when it is
run outside the cluster, and the host's own instance metadata. It mutates
nothing — not Galaxy, not its database, not its configuration.

| Information | Normal source | If unresolved |
| --- | --- | --- |
| Namespace and Galaxy release | The Galaxy deployment's own labels | Asked for only when several Galaxy releases exist |
| Source service, database and credential Secret | Galaxy's PostgreSQL resource: its application owner, Secret and database | An explicit source override; no hand-assembled DSN |
| Shared Galaxy account | Galaxy's configured single or shared account | A username, numeric ID or exact configured email |
| Public path | The existing Galaxy ingress prefix plus `/costs` | An explicit path override |
| Instance identity | A UUID generated once and kept in Rainstone's database | Explicit re-enrolment for a new source; the slug is a display convenience |
| Host project, numeric ID, shape, zone and region | The Galaxy VM's instance metadata | A small descriptor override, with unresolved fields visible |
| Batch project and location | Galaxy's own Batch runner configuration | Supplied explicitly; the host VM's project and region are not assumed to be Batch's |
| Persistent storage | The storage class Galaxy's own database persists on | A confirmed persistent location, never a silent default |

The output carries Secret *references*, never Secret contents, so no credential
passes through the description, a log or a rendered value. Only the Galaxy
configuration fields named above are parsed; job command lines, destination
parameters and arbitrary configuration are not read or emitted.

A field that does not resolve is reported with the reason and left out of the
values, so `helm install` fails on a missing prerequisite instead of installing
a deployment that reports against a guess. Later, k8s-boot can supply the same
description directly, so there is one deployment description rather than two
implementations.

### Reaching it from Galaxy

The chart registers Rainstone's path on the existing ingress, next to Galaxy's
own. On a platform whose proxy routes the Galaxy prefix, that path is reached
through the proxy like any other — a URL typed into the address bar arrives
without the headers the route requires, so Rainstone is entered from Galaxy.

Galaxy links sibling applications from its masthead through a webhook, which is
how the Kubernetes monitor is surfaced. The chart ships that webhook as its own
ConfigMap, so the link and icon live with the chart that knows the path.
Mounting it is Galaxy's deployment's job — only that deployment can add a volume
to Galaxy's pod — so Galaxy's release needs one extra volume and mount, marked
optional so either application can be installed first:

```yaml
extraVolumes:
  - name: webhook-rainstone
    configMap:
      name: rainstone-webhook
      optional: true
extraVolumeMounts:
  - name: webhook-rainstone
    mountPath: /galaxy/server/config/plugins/webhooks-custom/rainstone
    readOnly: true
```

Galaxy loads webhooks at startup, so its web pod picks the link up on its next
restart. This needs no change to the Galaxy chart itself: it is the same
`extraVolumes`/`extraVolumeMounts` pair the monitor already uses.

## Two database connections

The source connection reads Galaxy. The application connection writes
Rainstone's own database. They are separate databases and separate credentials.

The default profile reads Galaxy with a credential that already exists — the
application credential Galaxy itself uses — referenced as a Secret plus the
service, database and TLS mode it connects to. A complete DSN Secret is accepted
instead, for standalone deployments that keep one. URLs are assembled by the
driver's URL builder, so a password with URL metacharacters and any connection
options keep their own escaping. Credentials never appear in discovery output,
logs or generated values.

This credential retains Galaxy's own privileges. Rainstone confines its use to
read-only transactions, allowlisted statements naming their columns, a statement
timeout and a bounded connection pool — **application-enforced read-only
behavior, not a privilege boundary the database enforces**. The status report
states this rather than implying a boundary that does not exist. The web process
never receives this credential.

Galaxy's PostgreSQL superuser Secret is deliberately not used. Ordinary
collection needs no administrator credential.

### Optional: a provisioned reader

Where an installer is permitted to run privileged DDL, `source.provisionReader`
creates a dedicated login role with column-level SELECT grants and publishes its
credential to the Secret the collector mounts, so the database enforces the
boundary. It is a separate, explicit mode, off by default, and it creates no
Rainstone tables in Galaxy's database.

Publication is part of provisioning. Because the reader role commits to the
source database before its credential is published, an interrupted run would
otherwise leave the collector without a usable credential. Each run therefore
reconciles the two: it reads back what is published, tests whether it still
opens a session, rotates and republishes when it does not, verifies what was
stored, and fails rather than reporting success without a working credential. A
healthy installation re-runs without rotating anything.

```console
rainstone bootstrap \
    --admin-database-url postgresql+psycopg://…@galaxy-postgres-rw:5432/galaxy \
    --write-dsn /secrets/dsn
```

Use Galaxy's primary database service. On a single-instance deployment the
replica-only `-ro` service can have no endpoints, so a service named "read-only"
is not sufficient; discovery derives the hostname from the deployment's own
PostgreSQL resource.

## Source enrolment

A source lifetime is a UUID generated once and kept in Rainstone's own database.
Reading Galaxy therefore requires no schema, no identity table and no write of
any kind there.

```console
rainstone enroll --shared-account <galaxy account>
```

Endpoint, an oldest-job fingerprint and any non-secret deployment evidence are
recorded alongside it. They corroborate that the configured endpoint still holds
the enrolled database; none of them is identity:

- a restart, resize or renamed service reaches the same database, so a changed
  endpoint is recorded and collection continues;
- a changed oldest-job fingerprint is strong enough to stop on, because
  continuing would merge two databases' job IDs, attempts, invocation
  memberships and cursors into one history.

Be clear about the limit: without a marker inside the source or an authoritative
deployment event, a read-only client cannot reliably detect every silent restore
or replacement behind the same endpoint and storage identity. That is a
heuristic, not proof. Replacing the source is therefore an explicit act:

```console
rainstone enroll --shared-account <galaxy account> --replace-source
```

Re-enrolment retires the current fact namespace under a suffixed slug — its
history stays readable — and opens a new one with fresh cursors and its own
enrolment UUID. Overlapping job, user and workflow IDs from two sources are
never combined. Where the platform integration knows a replacement happened, it
should pass that event rather than leaving it to be noticed.

## Chart

`chart/` deploys the web and collector as separate workloads from the same
versioned image, so they restart independently and hold different credentials.

- The web process serves reports and holds no Galaxy source credential and no
  cloud observation permissions.
- The collector holds the source credential, uses in-cluster Kubernetes
  authentication, and reads cloud APIs with the identity this deployment already
  has. An operator kubeconfig is never mounted.
- Web, collector and initialization run under separate service accounts, and
  observation permissions belong to the collector. That separates *Kubernetes*
  identities. It is not cloud credential isolation: on a node whose metadata
  server is reachable from any pod, that node-wide credential is available
  regardless of service accounts. That inherited trust model is a property of
  the platform, and changing the VM or metadata network is not a prerequisite
  this chart imposes on installation.
- Rainstone's database is either an existing DSN Secret or the chart's own
  PostgreSQL deployment, in its own data directory. It never shares Galaxy's
  PostgreSQL data directory; two PostgreSQL processes must not.
- `database.storageClass` must name a storage class whose volumes survive stop,
  resume and reinstall. The chart refuses to render without that decision rather
  than landing on whatever the cluster's default happens to be, because
  reporting history and the enrolment identity live there. Its volume and
  credential are annotated `helm.sh/resource-policy: keep`, so both survive
  `helm uninstall` unless removed deliberately. Persistence is not backup.
- Migrations run as an explicit one-shot Job on install and upgrade, before new
  application pods become ready.
- The service is ClusterIP and the ingress registers the Galaxy-derived path.
  The chart adds no NodePort, LoadBalancer, public address or extra listener.
  The optional NetworkPolicy restricts in-cluster callers only; it cannot prove
  that a request arrived through the platform's authenticated route.
- Collector RBAC is observation only: get, list and watch on pods, batch jobs
  and nodes. No log or exec access.

Operator-facing settings stay small: where Rainstone runs, where its data lives,
retention, and an optional price feed. No one configures individual SKU prices
or per-region rates.

## Initialization sequence

Install and upgrade run one ordered sequence, and every step is idempotent, so
an interrupted run is simply re-run:

1. wait for the Rainstone database;
2. `alembic upgrade head`;
3. optionally `rainstone bootstrap`, when reader provisioning is enabled;
4. `rainstone enroll` — record the source enrolment, resolve the shared Galaxy
   account and seed the instance and baseline policy;
5. import or refresh the price catalog;
6. record a self-check in the installation context.

Initialization is an ordinary release resource, not a Helm hook. A post-install
hook would deadlock under `helm install --wait`: Helm waits for the deployments,
whose pods wait on `rainstone wait-ready`, which waits for the hook. As a normal
Job it is created alongside the deployments and runs while their init containers
wait, so `--wait` converges. Each revision gets its own Job name, and finished
Jobs are cleaned up by their TTL.

Readiness also waits for *this release's* initialization marker, not only for a
compatible schema: an upgrade that changes no migration would otherwise look
ready before its own initialization had run.

### Readiness and liveness

Readiness reflects a compatible schema and a resolved fixed scope: `/api/ready`
returns 503 with reasons until migrations match this release and the configured
shared account resolves to exactly one owner. Liveness stays cheap and
independent — `/api/health` for the web process, and a loop heartbeat
(`rainstone heartbeat`) for the collector. A denied cloud API or an unreachable
source degrades coverage and backs off; it does not restart collection from
healthy sources.

## Cloud reads

Cloud APIs are read with the credential this deployment's identity already has:
the VM's metadata-issued credential, or Workload Identity where a cluster
already uses it. Workload Identity is an alternative, not a prerequisite, and
installing Rainstone changes no IAM binding.

Batch observation follows Galaxy's own runner configuration, not the absence of
a Workload Identity annotation and not which runner recent jobs happened to use.
Where a Batch runner exists, its project and location come from that
configuration: Batch need not run in the host VM's project or region. Required
reads are probed with the collector's actual identity, and a denied read keeps
the Galaxy-derived information and shows the missing enrichment explicitly
rather than suppressing the cost or changing IAM automatically.

The baseline is the Galaxy server that is already running. Its numeric ID,
machine type and zone come from the host's instance metadata, so a node without
a Kubernetes `providerID` does not make them unknowable. Kubernetes placement
must agree with the runner and destination mapping before a job is classified as
using existing capacity.

Discovery names as baseline runners only runners that load Galaxy's local
runner, because only they run inside the Galaxy server's own machine; Batch and
Pulsar provision their own capacity, and a Kubernetes pod is placed on the
server only when Kubernetes observation shows it there. The destinations that
use those runners are named too, and a destination must be listed to match: a
runner alone places only work Galaxy recorded without a destination. When
destinations are assigned dynamically, for example by TPV, discovery reports
them as unresolved and they must be named explicitly.

The baseline assumptions hold for a declared period,
`RAINSTONE_BASELINE_EFFECTIVE_FROM` and optionally
`RAINSTONE_BASELINE_EFFECTIVE_TO` (`baseline.effectiveFrom` and
`baseline.effectiveTo` in the chart). The first time a period is seen it is saved
with the policy version, and from then on ordinary collection and
reclassification both apply the saved period, even if configuration stops
declaring it. Work outside it is never classified as baseline. A different
period under the same version is refused: nothing is classified, the
`baseline_period` self-check fails and `reclassify-baseline` exits without
changes. Changing the period needs a new policy version. With no period
declared or saved, the assumptions apply to all work and the self-check warns.

The `baseline_coverage` self-check compares the profile with the placements
Galaxy recorded. It warns when the profile matches nothing, when a baseline
runner recorded destinations that are not listed, and when Kubernetes ran pods
for a baseline runner. Classification happens as Galaxy records arrive, so
after correcting the profile run `rainstone reclassify-baseline`: it takes the
collector's lease, re-applies the profile to retained Galaxy records whose
placement no provider observation establishes, and recalculates. Work the
corrected profile no longer covers loses its baseline association and becomes
unavailable rather than zero; placement that was never observed is not
reconstructed. Server pricing stays separate from that relationship: a
job can add a known-zero amount of extra compute even when the server's own
price is unavailable, and neither uptime nor a zero is invented when placement
or policy is unknown.

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

Recorded findings come in two kinds. The collector is expected continuously, so
its report ages into warnings. Installation runs once: its findings are kept as
history, never decide current health, and are superseded by a fresh collector
check of the same capability — so a failure fixed after installation does not
linger as a permanent fault.

Cloud probes distinguish outcomes that used to look alike: `denied` (the
deployment identity lacks the permission), `unavailable` (the API could not be
reached) and `ok` — a missing resource is a valid answer from an API that
answered, not a pass for an API that refused. Every operation collection
performs is probed, including the gets and task listings, because list
permission does not imply them. Optional enrichment that is denied degrades
coverage with a visible gap rather than failing collection.

Findings name capability gaps without exposing DSNs, credentials, tokens, raw
job parameters or arbitrary logs, so the report can be downloaded through the
normal authenticated route from the Status view. Read-only checks run
automatically; submitting synthetic jobs is an opt-in development action.

## Current limitations

- Price coverage is a pilot: the bundled artifact declares `us-central1` only,
  from a 2026-09-19 snapshot captured by hand from the official pricing page. A
  provider catalog is meant to carry every region for the families it supports,
  maintained by a publisher that fetches official data and signs versioned
  artifacts. That publisher is not yet operated, so shapes elsewhere — including
  the observed `us-east4` N2 shapes — stay visibly unpriced rather than
  borrowing another region's rate, and this release cannot claim unattended
  current-price reporting. Enabling a feed requires `catalog.trustedKeys`; the
  chart refuses to render a feed URL without one, and a downloaded artifact
  always needs a verified signature regardless of settings.
- The Galaxy server's own running cost is never reported on a real
  installation. That server runs whether or not anyone submits a job, and the
  Server view exists to show what it costs over the period being viewed.
  Nothing in collection produces those figures, though: they come only from the
  bundled demonstration data, so on a real installation the view is empty
  rather than wrong. This is a missing feature, not a missing price — the
  server's own shape is in the catalog. Job reporting is unaffected: work that
  ran on that server is still reported as adding no extra compute cost, which
  is calculated separately and does come from live observation.
- The AnVIL dev pilot, live Leo-route validation, and stop/resume and reinstall
  recovery exercises are not yet done. The chart is verified by rendering,
  linting and the test suite: a real `helm install --wait`, a populated upgrade,
  recovery from an interrupted initialization, and a re-enrolment that keeps two
  histories apart are still outstanding against a live deployment.
- Billing reconciliation, Spot and preemption completeness, and hosted transport
  remain separately scoped.
