import type { AuditEntry } from "@/lib/mock-data";

// audit-timeline (partial): traceable actor + action log. Transition ≠ record —
// this is the record side (audit log), kept distinct from domain transitions.
export function AuditTimeline({ entries }: { entries: AuditEntry[] }) {
  return (
    <ol className="relative flex flex-col gap-ds-16 px-ds-16 py-ds-16">
      {entries.map((e, i) => (
        <li key={e.id} className="flex gap-ds-12">
          <div className="flex flex-col items-center">
            <span
              aria-hidden
              className="h-2.5 w-2.5 rounded-pill border-2 border-[var(--ds-color-primary)] bg-[var(--ds-color-surface)]"
            />
            {i < entries.length - 1 && (
              <span
                aria-hidden
                className="mt-ds-2 w-px flex-1 bg-[var(--ds-color-border)]"
              />
            )}
          </div>
          <div className="min-w-0 flex-1 pb-ds-4">
            <div className="flex flex-wrap items-center gap-ds-8">
              <span className="font-mono text-xs text-[var(--ds-color-ink-subtle)]">
                {e.time}
              </span>
              <span className="text-xs font-medium text-[var(--ds-color-ink-muted)]">
                {e.actor}
              </span>
            </div>
            <p className="mt-ds-2 text-sm text-[var(--ds-color-ink)]">
              {e.action}
            </p>
          </div>
        </li>
      ))}
    </ol>
  );
}
