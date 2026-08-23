import type { ReactNode } from "react";

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border">
        <div className="container flex items-center justify-between gap-6 py-4">
          <div>
            <h1 className="text-lg font-semibold tracking-tight">Project Sentinel</h1>
            <p className="text-xs text-muted-foreground">AI-powered project documentation analyst</p>
          </div>
          <nav className="hidden items-center gap-4 text-sm md:flex">
            <a className="font-medium text-foreground" href="#review">Review</a>
            <a className="text-muted-foreground hover:text-foreground" href="#report">Report</a>
            <a className="text-muted-foreground hover:text-foreground" href="#history">History</a>
          </nav>
        </div>
      </header>
      <main className="container py-8">{children}</main>
    </div>
  );
}
