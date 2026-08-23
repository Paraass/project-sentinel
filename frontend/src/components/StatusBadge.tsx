import { cn } from "@/lib/utils";

export function StatusBadge({ state }: { state: string }) {
  const isTerminalGood =
    state === "COMPLETED" || state === "REPORT_COMMITTED";

  const isWaiting =
    state === "AWAITING_HUMAN_REVIEW" || state === "WATCHING";

  const isFailed = state === "FAILED";

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md border px-2 py-1 text-xs font-medium",
        isFailed &&
          "border-destructive/30 bg-destructive/10 text-destructive",
        isTerminalGood &&
          "border-primary/30 bg-primary/10 text-primary",
        isWaiting &&
          "border-accent-foreground/20 bg-accent text-accent-foreground",
        !isFailed &&
          !isTerminalGood &&
          !isWaiting &&
          "border-border bg-muted text-muted-foreground",
      )}
    >
      {state}
    </span>
  );
}
