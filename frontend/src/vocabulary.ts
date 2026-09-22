/**
 * Presentation vocabulary.
 *
 * The API keeps stable machine-readable values; everything a scientist reads is
 * translated here. Accounting distinctions are preserved: zero, a small
 * positive amount, an unknown cost and an incomplete subtotal stay different
 * things, they are simply said in ordinary language.
 */

export const PRIMARY_MEASURE = "Estimated run compute cost";
export const PRIMARY_EXPLANATION =
  "Compute started for your tool and workflow runs. Your already-running Galaxy server is shown separately.";
export const ALLOCATION_MEASURE = "Resource allocation estimate";
export const ALLOCATION_EXPLANATION =
  "A share of the capacity your runs occupied, priced at public rates. It answers a different question from run compute cost and is never added to it.";
export const SERVER_EXPLANATION =
  "Your Galaxy server keeps running between jobs. This is its observed cost for the window below, not a share of any run.";
export const EXISTING_SERVER_SENTENCE =
  "This run used your already-running Galaxy server, so it added no compute charge. The server continues to incur costs.";

export function measureName(basis: string): string {
  return basis === "allocated" ? ALLOCATION_MEASURE : PRIMARY_MEASURE;
}

export function measureExplanation(basis: string): string {
  return basis === "allocated" ? ALLOCATION_EXPLANATION : PRIMARY_EXPLANATION;
}

/** Readable money. Details keep the exact decimal string. */
export function formatCost(amount: string | null | undefined): string {
  if (amount === null || amount === undefined || amount === "") return "Not available";
  const value = Number(amount);
  if (!Number.isFinite(value)) return "Not available";
  if (value === 0) return "$0.00";
  if (value > 0 && value < 0.01) return "less than $0.01";
  return value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  });
}

const RUN_STATUS: Record<string, string> = {
  completed: "Completed",
  failed: "Failed",
  running: "Running",
  cancelled: "Cancelled",
  "no runs recorded": "No runs recorded",
};

export function runStatusLabel(status: string): string {
  return RUN_STATUS[status] || status;
}

const JOB_STATE: Record<string, string> = {
  ok: "Completed",
  error: "Failed",
  failed: "Failed",
  running: "Running",
  queued: "Queued",
  new: "Not started",
  paused: "Paused",
  deleted: "Deleted",
  resubmitted: "Restarted",
};

export function jobStateLabel(state: string): string {
  return JOB_STATE[state] || state;
}

const CAPACITY: Record<string, string> = {
  existing: "Your Galaxy server",
  dedicated: "Dedicated cloud compute",
  elastic_shared: "Shared cloud capacity",
  unknown: "Not established",
};

/** Where the work ran, stated only when the resource relationship is verified. */
export function capacityLabel(capacities: string[] | undefined): string {
  if (!capacities || !capacities.length) return "Not established";
  const named = capacities.map(value => CAPACITY[value] || value);
  return [...new Set(named)].join(" and ");
}

const QUALITY: Record<string, string> = {
  known_zero: "$0 extra compute · Used your Galaxy server",
  complete: "Estimated",
  approximate: "Estimated",
  partial: "Cost incomplete",
  unpriced: "Price unavailable",
  in_progress: "Still running",
};

export function qualityLabel(quality: string): string {
  return QUALITY[quality] || quality;
}

/** One sentence explaining an amount, in the user's terms. */
export function costExplanation(record: {
  quality: string;
  amount: string | null;
  reason?: string;
  capacities?: string[];
}): string {
  if (record.quality === "known_zero") return EXISTING_SERVER_SENTENCE;
  if (record.quality === "unpriced") {
    return "No published price covers this machine and region yet, so its cost is unavailable rather than zero.";
  }
  if (record.quality === "partial") {
    return "Some evidence for this run is still missing, so the amount shown is a subtotal.";
  }
  if (record.quality === "in_progress") return "This work is still running, so its cost is provisional.";
  return record.reason || "Estimated from observed execution using public prices.";
}

export function coverageSentence(jobs: number, incomplete: number): string {
  if (!jobs) return "No runs in this period.";
  if (!incomplete) return `${jobs} ${jobs === 1 ? "run" : "runs"} included.`;
  return `${incomplete} ${incomplete === 1 ? "run" : "runs"} still need cost data.`;
}

/** Grammar that stays correct at one. */
export function needsCostData(count: number): string {
  return `${pluralize(count, "step")} still ${count === 1 ? "needs" : "need"} cost data`;
}

export function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
}
