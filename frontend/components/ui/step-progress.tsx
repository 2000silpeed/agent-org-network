import { Check, Loader2, Circle } from "lucide-react";

export interface Step {
  id: string;
  label: string;
  state: "done" | "active" | "pending";
}

// step-progress (editorial): staged stepper. Current step uses icon + word +
// aria-current, not color alone.
export function StepProgress({ steps }: { steps: Step[] }) {
  return (
    <ol className="ds-scrollbar-thin flex items-center gap-ds-4 overflow-x-auto pb-ds-4">
      {steps.map((s, i) => (
        <li key={s.id} className="flex shrink-0 items-center gap-ds-4">
          <div
            aria-current={s.state === "active" ? "step" : undefined}
            className="flex items-center gap-ds-4 rounded-pill border px-ds-12 py-[5px] text-xs font-medium"
            style={{
              borderColor:
                s.state === "active"
                  ? "var(--ds-color-primary)"
                  : "var(--ds-color-border)",
              background:
                s.state === "active"
                  ? "var(--ds-color-surface-tint)"
                  : s.state === "done"
                    ? "var(--ds-color-surface)"
                    : "transparent",
              color:
                s.state === "pending"
                  ? "var(--ds-color-ink-subtle)"
                  : "var(--ds-color-ink)",
            }}
          >
            {s.state === "done" && (
              <Check
                aria-hidden
                className="h-[13px] w-[13px] text-[var(--ds-color-success)]"
              />
            )}
            {s.state === "active" && (
              <Loader2
                aria-hidden
                className="h-[13px] w-[13px] animate-ds-spin text-[var(--ds-color-info)]"
              />
            )}
            {s.state === "pending" && (
              <Circle
                aria-hidden
                className="h-[13px] w-[13px] text-[var(--ds-color-ink-subtle)]"
              />
            )}
            <span>
              {s.label}
              {s.state === "active" && (
                <span className="text-[var(--ds-color-ink-subtle)]"> (현재)</span>
              )}
            </span>
          </div>
          {i < steps.length - 1 && (
            <span
              aria-hidden
              className="h-px w-4 shrink-0 bg-[var(--ds-color-border)]"
            />
          )}
        </li>
      ))}
    </ol>
  );
}
