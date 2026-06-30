import { FlaskConical } from "lucide-react";

// Marker for surfaces still on mock data because the real route is not yet
// implemented backend-side (재평가 · SSE 라이브 피드 · 워커 admission). Keeps the
// demo honest: shows what is real vs. placeholder.
export function MockMarker({ note }: { note: string }) {
  return (
    <div
      role="note"
      className="flex items-start gap-ds-8 rounded-md border border-dashed border-[var(--ds-color-warning)] bg-[color-mix(in_srgb,var(--ds-color-warning)_8%,transparent)] px-ds-12 py-ds-8"
    >
      <FlaskConical
        aria-hidden
        className="mt-[2px] h-4 w-4 shrink-0 text-[var(--ds-color-warning)]"
      />
      <p className="text-xs text-[var(--ds-color-ink-muted)]">
        <span className="font-semibold text-[var(--ds-color-ink)]">실 API 후속</span> —{" "}
        {note} 아래는 목 데이터입니다.
      </p>
    </div>
  );
}
