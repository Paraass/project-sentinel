import { useEffect, useState } from "react";

import { EmptyState } from "@/components/EmptyState";
import { StatusBadge } from "@/components/StatusBadge";
import { ApiError, getRun, type RunResponse } from "@/lib/api";

type LoadState =
  | { status: "no-run-id" }
  | { status: "loading" }
  | { status: "success"; run: RunResponse }
  | { status: "error"; error: ApiError };

function readRunIdFromUrl(): string | null {
  const params = new URLSearchParams(window.location.search);
  return params.get("run_id");
}

function formatTimestamp(value: string): string {
  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleString();
}

export default function PileOverview() {
  const [state, setState] = useState<LoadState>(() =>
    readRunIdFromUrl()
      ? { status: "loading" }
      : { status: "no-run-id" },
  );

  useEffect(() => {
    const runId = readRunIdFromUrl();

    if (!runId) {
      setState({ status: "no-run-id" });
      return;
    }

    let cancelled = false;

    setState({ status: "loading" });

    getRun(runId)
      .then((run) => {
        if (!cancelled) {
          setState({ status: "success", run });
        }
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }

        if (error instanceof ApiError) {
          setState({
            status: "error",
            error,
          });
          return;
        }

        setState({
          status: "error",
          error: new ApiError(
            500,
            "Unexpected error while loading the run.",
          ),
        });
      });

    return () => {
      cancelled = true;
    };
  }, []);

  if (state.status === "no-run-id") {
    return (
      <EmptyState
        title="No run selected"
        description="Provide a run_id in the URL to view a pile."
      />
    );
  }

  if (state.status === "loading") {
    return (
      <EmptyState
        title="Loading run"
        description="Fetching the selected pile run."
      />
    );
  }

  if (state.status === "error") {
    return (
      <EmptyState
        title={
          state.error.status === 404
            ? "Run not found"
            : "Unable to load run"
        }
        description={state.error.message}
      />
    );
  }

  const { run } = state;

  return (
    <section className="space-y-6">
      <div>
        <p className="text-sm font-medium text-muted-foreground">
          Pile overview
        </p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">
          Run details
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Review the current state of the selected processing run.
        </p>
      </div>

      <div className="rounded-lg border border-border bg-background p-6">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Run ID
            </p>
            <p className="mt-1 break-all font-mono text-sm text-foreground">
              {run.run_id}
            </p>
          </div>

          <StatusBadge state={run.current_state} />
        </div>
      </div>

      <div className="rounded-lg border border-border bg-background">
        <dl className="divide-y divide-border">
          <div className="grid gap-1 px-6 py-4 sm:grid-cols-3 sm:gap-4">
            <dt className="text-sm font-medium text-muted-foreground">
              Status
            </dt>
            <dd className="text-sm text-foreground sm:col-span-2">
              <StatusBadge state={run.current_state} />
            </dd>
          </div>

          <div className="grid gap-1 px-6 py-4 sm:grid-cols-3 sm:gap-4">
            <dt className="text-sm font-medium text-muted-foreground">
              Last updated
            </dt>
            <dd className="text-sm text-foreground sm:col-span-2">
              {formatTimestamp(run.updated_at)}
            </dd>
          </div>
        </dl>
      </div>
    </section>
  );
}