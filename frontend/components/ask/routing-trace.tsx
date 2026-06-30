import { Check, Loader2, Circle } from "lucide-react";
import type { RoutingStep } from "@/lib/mock-data";

// Staged routing trace: "담당 찾는 중 → cs_ops 전달됨 → 답변 작성 중".
// Shows owner-facing stages only. Never exposes scores or candidate math.
export function RoutingTrace({ steps }: { steps: RoutingStep[] }) {
  return (
    <ol
      aria-label="라우팅 진행"
      className="flex flex-wrap items-center gap-x-ds-8 gap-y-ds-4 text-xs"
    >
      {steps.map((s, i) => (
        <li key={s.id} className="flex items-center gap-ds-4">
          {s.state === "done" && (
            <Check
              aria-hidden
              className="h-[14px] w-[14px] text-[var(--ds-color-success)]"
            />
          )}
          {s.state === "active" && (
            <Loader2
              aria-hidden
              className="h-[14px] w-[14px] animate-ds-spin text-[var(--ds-color-info)]"
            />
          )}
          {s.state === "pending" && (
            <Circle
              aria-hidden
              className="h-[14px] w-[14px] text-[var(--ds-color-ink-subtle)]"
            />
          )}
          <span
            className={
              s.state === "pending"
                ? "text-[var(--ds-color-ink-subtle)]"
                : "text-[var(--ds-color-ink-muted)]"
            }
          >
            {s.label}
            <span className="sr-only">
              {s.state === "done"
                ? " 완료"
                : s.state === "active"
                  ? " 진행 중"
                  : " 대기"}
            </span>
          </span>
          {i < steps.length - 1 && (
            <span aria-hidden className="text-[var(--ds-color-ink-subtle)]">
              →
            </span>
          )}
        </li>
      ))}
    </ol>
  );
}
