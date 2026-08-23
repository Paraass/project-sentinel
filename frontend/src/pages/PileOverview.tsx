import { useCallback, useEffect, useMemo, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { StatusBadge } from "@/components/StatusBadge";
import {
  ApiError,
  closeReview,
  decideReviewItem,
  getChangelog,
  getCurrentReport,
  getReviewItems,
  getRun,
  type ChangelogEntryResponse,
  type ReportResponse,
  type ReviewItemResponse,
  type RunResponse,
} from "@/lib/api";

type LoadState =
  | { status: "no-run-id" }
  | { status: "loading" }
  | { status: "success"; run: RunResponse }
  | { status: "error"; error: ApiError };

function readRunIdFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get("run_id");
}

function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function humanState(state: string): string {
  return state.replace(/_/g, " ").toLowerCase().replace(/(^|\s)/g, (c: string) => c.toUpperCase());
}

function ReviewCard({
  item,
  busy,
  onDecision,
}: {
  item: ReviewItemResponse;
  busy: boolean;
  onDecision: (item: ReviewItemResponse, decision: "approve" | "reject" | "defer", reason: string) => void;
}) {
  const [reason, setReason] = useState(item.decision_reason ?? "");
  const [expanded, setExpanded] = useState(true);
  const decided = item.decision !== "PENDING";

  return (
    <article className="rounded-lg border border-border bg-card p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="font-medium">{humanState(item.item_type)}</h3>
            <StatusBadge state={item.decision} />
          </div>
          <p className="mt-1 font-mono text-xs text-muted-foreground">{item.source_reference}</p>
        </div>
        <button
          type="button"
          className="text-xs text-muted-foreground underline underline-offset-4"
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Collapse" : "Expand"}
        </button>
      </div>

      {expanded && (
        <div className="mt-4 space-y-4">
          <pre className="max-h-72 overflow-auto rounded-md bg-muted p-4 text-xs leading-5 whitespace-pre-wrap">
            {prettyJson(item.content)}
          </pre>
          <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
            <input
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              disabled={busy || decided}
              placeholder="Decision reason (optional)"
              className="min-w-0 rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring"
            />
            <div className="flex flex-wrap gap-2">
              <button type="button" disabled={busy || decided} onClick={() => onDecision(item, "approve", reason)} className="rounded-md border border-primary/30 bg-primary/10 px-3 py-2 text-sm font-medium disabled:opacity-50">Approve</button>
              <button type="button" disabled={busy || decided} onClick={() => onDecision(item, "reject", reason)} className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm font-medium disabled:opacity-50">Reject</button>
              <button type="button" disabled={busy || decided} onClick={() => onDecision(item, "defer", reason)} className="rounded-md border border-border bg-muted px-3 py-2 text-sm font-medium disabled:opacity-50">Defer</button>
            </div>
          </div>
          {item.decided_by && (
            <p className="text-xs text-muted-foreground">
              Decided by {item.decided_by} at {formatTimestamp(item.decided_at)}
              {item.decision_reason ? ` · ${item.decision_reason}` : ""}
            </p>
          )}
        </div>
      )}
    </article>
  );
}

export default function PileOverview() {
  const runId = readRunIdFromUrl();
  const [state, setState] = useState<LoadState>(() => runId ? { status: "loading" } : { status: "no-run-id" });
  const [reviewItems, setReviewItems] = useState<ReviewItemResponse[]>([]);
  const [report, setReport] = useState<ReportResponse | null>(null);
  const [changelog, setChangelog] = useState<ChangelogEntryResponse[]>([]);
  const [busyItem, setBusyItem] = useState<string | null>(null);
  const [closing, setClosing] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    if (!runId) return;
    setMessage(null);
    const [run, items, currentReport, entries] = await Promise.all([
      getRun(runId),
      getReviewItems(runId),
      getCurrentReport(runId).catch((error: unknown) => error instanceof ApiError && error.status === 404 ? null : Promise.reject(error)),
      getChangelog(runId),
    ]);
    setState({ status: "success", run });
    setReviewItems(items);
    setReport(currentReport);
    setChangelog(entries);
  }, [runId]);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    loadData().catch((error: unknown) => {
      if (cancelled) return;
      const apiError = error instanceof ApiError ? error : new ApiError(0, "An unexpected error occurred.");
      setState({ status: "error", error: apiError });
    });
    return () => { cancelled = true; };
  }, [runId, loadData]);

  const pendingCount = useMemo(() => reviewItems.filter((item) => item.decision === "PENDING").length, [reviewItems]);

  async function handleDecision(item: ReviewItemResponse, decision: "approve" | "reject" | "defer", reason: string) {
    setBusyItem(item.id);
    setMessage(null);
    try {
      const updated = await decideReviewItem(item.id, decision, "reviewer", reason);
      setReviewItems((items) => items.map((candidate) => candidate.id === updated.id ? updated : candidate));
    } catch (error: unknown) {
      setMessage(error instanceof ApiError ? error.message : "Could not save the decision.");
    } finally {
      setBusyItem(null);
    }
  }

  async function handleCloseReview() {
    if (!runId) return;
    setClosing(true);
    setMessage(null);
    try {
      const updated = await closeReview(runId);
      setState({ status: "success", run: updated });
      await loadData();
    } catch (error: unknown) {
      setMessage(error instanceof ApiError ? error.message : "Could not close the review.");
    } finally {
      setClosing(false);
    }
  }

  if (state.status === "no-run-id") {
    return <EmptyState title="No run selected" description="Add ?run_id=<UUID> to the URL to view a specific run." />;
  }
  if (state.status === "loading") {
    return <div className="rounded-lg border border-border p-8 text-center"><p className="text-sm text-muted-foreground">Loading run…</p></div>;
  }
  if (state.status === "error") {
    const isMissing = state.error.status === 404;
    return <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-8 text-center"><p className="text-sm font-medium text-destructive">{isMissing ? "Run not found" : "Could not load this run"}</p><p className="mt-1 text-sm text-muted-foreground">{state.error.message}</p></div>;
  }

  const { run } = state;
  const canClose = run.current_state === "AWAITING_HUMAN_REVIEW";

  return (
    <div className="space-y-8">
      <section className="flex flex-col gap-4 border-b border-border pb-6 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Document pile</p>
          <h2 className="mt-1 text-2xl font-semibold tracking-tight">{run.name ?? "Unnamed run"}</h2>
          <p className="mt-1 font-mono text-xs text-muted-foreground">{run.run_id}</p>
        </div>
        <div className="flex items-center gap-3">
          <StatusBadge state={run.current_state} />
          <button type="button" onClick={() => void loadData()} className="rounded-md border border-border bg-background px-3 py-2 text-sm font-medium">Refresh</button>
        </div>
      </section>

      {message && <div className="rounded-md border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">{message}</div>}

      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-lg border border-border p-4"><p className="text-xs uppercase tracking-wide text-muted-foreground">Documents</p><p className="mt-2 text-2xl font-semibold">{run.document_count}</p></div>
        <div className="rounded-lg border border-border p-4"><p className="text-xs uppercase tracking-wide text-muted-foreground">Pending review</p><p className="mt-2 text-2xl font-semibold">{pendingCount}</p></div>
        <div className="rounded-lg border border-border p-4"><p className="text-xs uppercase tracking-wide text-muted-foreground">Report</p><p className="mt-2 text-2xl font-semibold">{report ? `v${report.version}` : "—"}</p></div>
        <div className="rounded-lg border border-border p-4"><p className="text-xs uppercase tracking-wide text-muted-foreground">Resume stage</p><p className="mt-2 text-sm font-medium">{humanState(run.resume_stage)}</p></div>
      </section>

      <section id="review" className="space-y-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div><h3 className="text-lg font-semibold">Review queue</h3><p className="text-sm text-muted-foreground">Every finding, conflict, and proposal stays independently reviewable.</p></div>
          <button type="button" disabled={!canClose || closing} onClick={() => void handleCloseReview()} className="rounded-md border border-primary/30 bg-primary/10 px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50">
            {closing ? "Committing…" : "Close review & commit"}
          </button>
        </div>
        {reviewItems.length === 0 ? (
          <EmptyState title="No review items" description="This run currently has no persisted review items." />
        ) : (
          <div className="space-y-3">
            {reviewItems.map((item) => <ReviewCard key={item.id} item={item} busy={busyItem === item.id} onDecision={handleDecision} />)}
          </div>
        )}
      </section>

      <section id="report" className="space-y-4">
        <div><h3 className="text-lg font-semibold">Current report</h3><p className="text-sm text-muted-foreground">Only a committed report is shown as authoritative.</p></div>
        {report ? (
          <div className="rounded-lg border border-border p-5">
            <div className="mb-4 flex items-center justify-between"><StatusBadge state={`VERSION_${report.version}`} /><span className="text-xs text-muted-foreground">{formatTimestamp(report.created_at)}</span></div>
            <pre className="max-h-[32rem] overflow-auto rounded-md bg-muted p-4 text-xs leading-5 whitespace-pre-wrap">{prettyJson(report.content)}</pre>
          </div>
        ) : <EmptyState title="No committed report" description="The backend has not committed a report for this run yet." />}
      </section>

      <section id="history" className="space-y-4">
        <div><h3 className="text-lg font-semibold">Version history</h3><p className="text-sm text-muted-foreground">Persisted changelog entries provide the audit trail for committed versions.</p></div>
        {changelog.length === 0 ? <EmptyState title="No changelog entries" description="Nothing has been committed for this run yet." /> : (
          <div className="space-y-3">
            {changelog.map((entry) => (
              <article key={entry.id} className="rounded-lg border border-border p-4">
                <div className="flex items-center justify-between gap-4"><span className="font-medium">Report v{entry.report_version}</span><span className="text-xs text-muted-foreground">{formatTimestamp(entry.created_at)}</span></div>
                <p className="mt-2 text-sm">{entry.summary}</p>
                {entry.source_document_ids.length > 0 && <p className="mt-2 text-xs text-muted-foreground">Sources: {entry.source_document_ids.join(", ")}</p>}
                {entry.affected_claim_ids.length > 0 && <p className="mt-1 text-xs text-muted-foreground">Affected claims: {entry.affected_claim_ids.join(", ")}</p>}
              </article>
            ))}
          </div>
        )}
      </section>

      <p className="border-t border-border pt-4 text-xs text-muted-foreground">Created {formatTimestamp(run.created_at)} · Updated {formatTimestamp(run.updated_at)}</p>
    </div>
  );
}
