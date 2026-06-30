import type { ConsoleMetric } from "@/lib/mock-data";
import { StatusBadge } from "@/components/ui/status-badge";

// stat-card: single raised surface, metric header + value + context.
// Not a homogeneous card wall — paired with feed/rows on the console.
export function StatCard({ metric }: { metric: ConsoleMetric }) {
  return (
    <div className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-16 py-ds-12">
      <div className="flex items-center justify-between gap-ds-8">
        <p className="text-xs font-medium text-[var(--ds-color-ink-subtle)]">
          {metric.label}
        </p>
        <StatusBadge tone={metric.tone} label={metric.tone === "warning" ? "주의" : "정상"} />
      </div>
      <p className="mt-ds-8 flex items-baseline gap-ds-4">
        <span className="font-heading text-2xl font-semibold tabular-nums text-[var(--ds-color-ink)]">
          {metric.value}
        </span>
        <span className="text-sm text-[var(--ds-color-ink-muted)]">
          {metric.unit}
        </span>
      </p>
      <p className="mt-ds-4 text-xs text-[var(--ds-color-ink-subtle)]">
        {metric.hint}
      </p>
    </div>
  );
}
