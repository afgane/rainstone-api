# Dashboard experience

The dashboard is built around three questions a scientist actually asks: what
did my work cost in a period, what did this workflow run cost, and which tools
account for most of it. Everything needed to answer them is on screen by
default; advanced reporting stays available but out of the way.

## Layout

One left sidebar carries navigation and the few controls a normal answer needs:
period buttons, one search box worded for the current page, and a collapsed
**More filters** section. There is no filter panel above the results and no cost
basis selector in the content area.

Navigation is **Overview**, **Workflow runs**, **Tool runs** and **Tools**, with
**Galaxy accounts** (administrator only), **Galaxy server** (when authorized)
and **Status** in a secondary group. Status carries no report controls.

Advanced filters that are active remain visible as removable chips while the
section is collapsed, so a hidden selection can never quietly change an easy
answer. Below 1000px the sidebar becomes a drawer behind a Filters button that
shows the active filter count. Report state stays in the URL, so a link
reproduces a view, and browser history, keyboard operation and focus
restoration all work.

## Periods

Period buttons are calendar periods in the reporting timezone, not rolling
windows: yesterday is the preceding calendar day, last week the previous Monday
to Sunday, last month the previous calendar month. "This week" and "this month"
run from their start to now and say "(so far)". The resolved dates are always
printed under the buttons, so a label is never ambiguous.

Users pick an inclusive last day; the API boundary is exclusive and the
conversion happens internally. Period amounts use cost accrued *within* the
period, including failed attempts and running work — never a completed-job
total substituted for "what did I spend yesterday".

## Words and money

| Internal value | What the user reads |
| --- | --- |
| `additional` basis | Estimated run compute cost, with "Compute started for your tool and workflow runs. Your already-running Galaxy server is shown separately." |
| `allocated` basis | Resource allocation estimate, with its own explanation |
| baseline infrastructure | Galaxy server cost, in a quieter section with its observed window |
| `known_zero` | $0 extra compute · Used your Galaxy server |
| `partial` / `unpriced` | Cost incomplete / Price unavailable, each with a reason |
| root invocation | Workflow run |
| `ok` / `error` / scheduling states | Completed, Failed, and a run status derived from the run's executions |
| `gcp_batch` / `kubernetes` | Dedicated cloud compute / Your Galaxy server, only where the resource relationship is established |
| full Tool Shed identifier | Tool name and version, with the full identity in details |

Amounts render to two decimals, show "less than $0.01" rather than rounding a
real cost to zero, and keep "Not available" distinct from `$0.00`. Exact decimal
values remain in details and CSV. Galaxy's database stores tool IDs rather than
display names, so the readable name is derived from the identity and the full
identity always travels with it; two tools with similar names stay
distinguishable.

## Runs versus periods

A workflow run's headline is its **run total**: the whole run, whatever period
is selected. When the selected period covers only part of it, the period share
is shown beside it rather than replacing it. The period selector scopes which
runs are listed — runs that accrued cost inside it, plus runs that started
inside it — and runs with unusable timing stay listed and marked.

Opening a run leads with the run total and its completion and coverage status,
then the tool steps, child workflows counted once, reused outputs and any steps
still missing cost data. A tool run's detail leads with its own cost, an
explanation in ordinary language, the resource it used (with a retry's shared
charge stated once), and its attempts.
