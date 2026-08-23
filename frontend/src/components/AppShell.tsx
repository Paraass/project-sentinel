import type { ReactNode } from "react";

export function AppShell({ children }: { children: ReactNode }) {
  const futureSurfaces = [
    "Intake",
    "Review Queue",
    "Report Viewer",
    "Version History",
    "Configuration",
  ];

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border">
        <div className="container flex items-center justify-between py-4">
          <div>
            <h1 className="text-lg font-semibold tracking-tight">
              Project Sentinel
            </h1>
            <p className="text-xs text-muted-foreground">
              AI-powered project documentation analyst
            </p>
          </div>

          <nav className="flex items-center gap-4">
            <span className="text-sm font-medium text-foreground">
              Pile Overview
            </span>

            {futureSurfaces.map((label) => (
              <span
                key={label}
                title="Not yet implemented"
                aria-disabled="true"
                className="cursor-not-allowed text-sm text-muted-foreground/50"
              >
                {label}
              </span>
            ))}
          </nav>
        </div>
      </header>

      <main className="container py-8">{children}</main>
    </div>
  );
}
