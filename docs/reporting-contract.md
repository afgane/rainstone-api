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
provisional. A zero-duration interval is an instant: it belongs whole to the
half-open range that contains it, so known-zero work that lasted no measurable
time keeps its zero in exactly one period.

Work whose cost evidence has no usable timing belongs to no period. A report
without a date range includes it and counts it as temporally unattributed. A
dated report leaves it out of totals, counts, rankings, workflow listings and
CSV export, and describes it separately: `meta.undated` carries its job count,
known subtotal and incomplete count, and the job list returns one page of it as
`undated_items`, paged by `undated_offset` independently of `offset`. Job creation time is never substituted for cost timing.

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
so no report repeats a whole-VM amount. `cost_lines` counts charged lifetimes.

Galaxy and each provider observe the same execution separately, so observations
are reconciled into logical attempts before anything is counted as a repeat.
When provider evidence exists, Galaxy's own record describes one of those
executions unless it carries a resource of its own. Parallel tasks of one
submission are not repeats; a later ordinal of the same task, or a later
submission of it, is. `attempt_count` counts logical attempts,
`repeat_attempt_count` the repeats among them, `observation_count` the raw
observations, and `attempt_evidence` says whether the count rests on provider
observations or on Galaxy's record alone, which cannot show a repeat. In a job
detail each attempt carries a `role` of `first`, `repeat` or `observation`.

The summary reports failed work (`failed_spend`, with
`failed_incomplete_job_count` for failed jobs still missing cost data) and two
different repeat figures: `repeated_job_spend` is the whole cost of jobs that
had a repeat attempt, and `repeat_attempt_spend` only what resources used
solely by repeats cost. A resource shared by a first attempt and its repeat
cannot be divided without a policy, so its amount is reported as
`repeat_attempt_shared_spend` and `repeat_attempt_spend_complete` is false.
A logical attempt with no resource evidence has a cost no line includes: its
job is `partial`, its reason says so, and when it is a repeat inside the report
period the repeat subtotal is incomplete. An attempt without timing counts as
inside every period, so a missing cost never drops out of all of them.

A job with no cost lines reports its evidence state, not an execution state:
`in_progress` while it is queued or running, `not_started` when it is new or
paused, and `unavailable` once it has finished. An unavailable job's reason says
whether Galaxy recorded when it ran; recalculating alone cannot recover
observations that were never collected.

A price that takes effect after work ran is never applied to it. Such work is
unpriced, and its reason names when the earliest published price takes effect.

The summary distinguishes the requested window from what was observed:
`observation_window` echoes the report bounds, while
`baseline_infrastructure_observed` (and the infrastructure report's
`observed_coverage`) is the span of server observations inside them, or null
when there are none. A tenant restored from a captured snapshot carries an
`imported_snapshot` capability, returned by the summary with its capture time,
source cutoffs and digest; it is real data and is not marked as a demo.

Raw job metrics are not part of the revision content digest: they reach reports
only through attempts, lifetimes and job resource hints, which are covered.

Collector health is reported separately from web health: `/api/freshness`
carries per-source status, cursors, lag and recorded observation gaps, and
`/api/status` carries the same read-only self-checks as `rainstone doctor`,
sanitized for download through the normal authenticated route.
