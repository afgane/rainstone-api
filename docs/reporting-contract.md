# Phase 2A reporting contract

All report endpoints accept the same query fields: cost basis, USD currency,
mode, half-open time interval, IANA timezone, search, full tool identity,
invocation, stable workflow identity, authorized owner, state, runner, destination, capacity relationship,
quality, cost range, calculation revision, pagination, and allowlisted sorting.
The initial page size is 50 and the maximum is 200. A unique internal job ID
breaks sort ties, and missing costs sort after known values in both directions.

The summary request chooses a calculation revision. The browser pins that
revision into every table, chart, detail, and export request and serializes it
in the URL. A revision records the tenant's report-generation marker, which
database triggers advance whenever tenant, owner, job, attempt, resource
lifetime, segment, association, policy, price, workflow membership, or
infrastructure facts change, including through a write that bypasses the
application. A revision that is no longer current, or whose marker has moved,
returns a conflict instead of joining its cost lines to newer mutable
observations. Revisions also record a content digest, so replaying identical
facts reuses the existing revision rather than creating another. Report
requests use a repeatable-read database snapshot so validation and aggregation
see one state.

The default accrued mode clips each observed resource interval to the selected
half-open range. A resource lifetime is split at catalog price boundaries. A
billable minimum is applied once and its uplift is distributed proportionally
over the observed positive-duration lifetime. Local calendar buckets
use the selected timezone through zoneinfo, including daylight-saving
transitions. Open intervals use the fixed revision timestamp and are
provisional. Amounts without usable timing remain temporally unattributed.

The completed mode selects successful completions in the interval, uses their
full-job amounts, and places each total on its final successful execution
completion day. Tool mean, median, and p95 exclude unsuccessful, incomplete,
and unknown executions and report their sample and excluded counts. The fixture
calculation uses continuous linear interpolation (R-7) for p95 and labels the
method. Statistics retain an approximation flag when lifecycle evidence is
approximate. Workflow totals
deduplicate job IDs and root totals never add children a second time.

Decimal amounts cross the API and CSV boundary as exact strings. Null means
unknown. Zero is an observed value. A partial amount is a known subtotal and may
also count as incomplete, so coverage categories are explicitly non-disjoint.
A report containing jobs but no known amounts has an unavailable total; a report
with no jobs is an empty result.

Owner and tenant scope is applied before filtering, aggregation, membership
expansion, and export. User and infrastructure reports require administrator
scope in the local multi-user profile. Infrastructure represents a separate
whole-resource scope; job, tool, and owner filters do not apportion it. CSV
exports read database-sortable reports in bounded 500-job batches, stream the
complete authorized filtered result, and neutralize cells that spreadsheet
software could interpret as formulas. Cost-sorted exports retain the in-memory
compatibility path because their ordering depends on calculated report amounts.

## Resource lifetimes in reports (Phase 2B)

Cost lines are keyed by chargeable resource lifetime and attributed job, not by
attempt. A job detail therefore reports two lists: `resources`, each with its
amount, shape, observed window, timing method and the attempts that shared it;
and `attempts`, each with Galaxy outcome, provider outcome, exit code, task
index and attempt ordinal. An attempt shows an amount only when it is the sole
user of that lifetime; otherwise the row names the attempts sharing the charge
so no report repeats a whole-VM amount. `cost_lines` counts charged lifetimes
and `attempt_count` counts observed attempts; they differ for a same-VM retry.

Raw job metrics are not part of the revision content digest: they reach reports
only through attempts, lifetimes and job resource hints, which are covered.

Collector health is reported separately from web health: `/api/freshness`
carries per-source status, cursors, lag and recorded observation gaps, and
`/api/status` carries the same read-only self-checks as `rainstone doctor`,
sanitized for download through the normal authenticated route.
