import { cn } from "@/lib/utils";

export default function Landing() {
  return (
    <main
      className={cn(
        "flex min-h-screen flex-col items-center justify-center bg-background text-foreground",
      )}
    >
      <div className="text-center">
        <h1 className="text-3xl font-semibold tracking-tight">Project Sentinel</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Repository foundation is running.
        </p>
      </div>
    </main>
  );
}
