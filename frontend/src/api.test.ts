import { beforeEach, describe, expect, it, vi } from "vitest";

import { collectionCutoff, loadReport, type ReportState } from "./api";

const SUMMARY = { revision_id: "rev-1", coverage: {}, amount: "0" };
const EMPTY_LIST = { items: [], total: 0, limit: 50, offset: 0, meta: {} };

function state(): ReportState {
  return {
    view: "overview", period: "this-month", basis: "additional", mode: "accrued",
    fromTime: "", toTime: "", timezone: "UTC", search: "", owner: "", toolId: "",
    toolVersion: "", invocationId: "", workflowId: "", state: "", runner: "",
    destination: "", capacity: "", quality: "", minCost: "", maxCost: "",
    sort: "created_at", direction: "desc", offset: 0,
  };
}

/** Records every request path, and answers whatever the endpoint needs. */
function respond(paths: string[], failFirstPinned = false) {
  let pinnedSeen = 0;
  return vi.fn(async (url: string) => {
    paths.push(url);
    const pinned = url.includes("revision=");
    if (pinned && failFirstPinned && ++pinnedSeen === 1) {
      return new Response(JSON.stringify({ detail: "This snapshot is stale" }), { status: 409 });
    }
    const body = url.includes("/summary")
      ? SUMMARY
      : url.includes("/freshness") || url.includes("/me")
        ? {}
        : EMPTY_LIST;
    return new Response(JSON.stringify(body), { status: 200 });
  });
}

describe("loadReport", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("asks for the latest revision, then holds it for the rest of the view", async () => {
    const paths: string[] = [];
    vi.stubGlobal("fetch", respond(paths));

    await loadReport(state());

    const [summary, ...rest] = paths;
    // The first call must not pin: a pin carried in from anywhere else would
    // make a reload ask for a revision that has since been superseded.
    expect(summary).toContain("/summary");
    expect(summary).not.toContain("revision=");
    // Every other part of the view comes from the revision that answered.
    const scoped = rest.filter(path => path.includes("?"));
    expect(scoped.length).toBeGreaterThan(0);
    expect(scoped.every(path => path.includes("revision=rev-1"))).toBe(true);
  });

  it("repeats the load when a recalculation supersedes its snapshot", async () => {
    const paths: string[] = [];
    vi.stubGlobal("fetch", respond(paths, true));

    const result = await loadReport(state());

    // A collector recalculating mid-load is routine, so the reader sees the
    // new snapshot rather than a stale-snapshot error.
    expect(result.summary).toMatchObject({ revision_id: "rev-1" });
    expect(paths.filter(path => path.includes("/summary")).length).toBe(2);
  });
});

describe("collection cutoff", () => {
  const source = (name: string, at: string | null, status = "healthy") => ({
    source: name, status, last_success_at: at, error: null,
  });

  it("is the oldest report source, ignoring the price catalog", () => {
    const result = collectionCutoff({
      overall_status: "healthy", observation_gaps: [],
      sources: [
        source("galaxy_db", "2026-09-23T02:40:16Z"),
        source("gcp_batch", "2026-09-23T02:39:23Z"),
        source("price_catalog", "2026-09-01T00:00:00Z", "historical_snapshot"),
      ],
    });
    expect(result).toEqual({ cutoff: "2026-09-23T02:39:23Z", stale: false });
  });

  it("is stale when any source is, and unknown when a source never succeeded", () => {
    expect(collectionCutoff({
      overall_status: "partial", observation_gaps: [],
      sources: [source("galaxy_db", "2026-09-23T02:40:16Z", "stale")],
    }).stale).toBe(true);
    expect(collectionCutoff({
      overall_status: "partial", observation_gaps: [], sources: [source("kubernetes", null)],
    })).toEqual({ cutoff: null, stale: true });
  });
});
