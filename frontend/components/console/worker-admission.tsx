import { Check, X, KeyRound } from "lucide-react";
import type { PendingWorker } from "@/lib/mock-data";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";

// Worker admission panel: pending workers with admission token, approve/cancel.
export function WorkerAdmission({ workers }: { workers: PendingWorker[] }) {
  if (workers.length === 0) {
    return (
      <div className="px-ds-16 py-ds-16 text-sm text-[var(--ds-color-ink-subtle)]">
        승인 대기 중인 워커가 없습니다.
      </div>
    );
  }
  return (
    <ul className="flex flex-col divide-y divide-[var(--ds-color-border)]">
      {workers.map((w) => (
        <li key={w.id} className="flex flex-col gap-ds-8 px-ds-16 py-ds-12">
          <div className="flex flex-wrap items-center gap-ds-8">
            <span className="font-medium text-[var(--ds-color-ink)]">
              {w.worker}
            </span>
            <span className="text-xs text-[var(--ds-color-ink-subtle)]">
              {w.domain}
            </span>
            <StatusBadge tone="pending" label="승인 대기" />
          </div>
          <div className="flex items-center gap-ds-4 text-xs text-[var(--ds-color-ink-muted)]">
            <KeyRound
              aria-hidden
              className="h-[14px] w-[14px] text-[var(--ds-color-ink-subtle)]"
            />
            <span className="font-mono">{w.admissionToken}</span>
            <span aria-hidden className="text-[var(--ds-color-ink-subtle)]">
              ·
            </span>
            <span>{w.requestedAt}</span>
          </div>
          <div className="flex flex-wrap items-center gap-ds-8">
            <Button size="sm" variant="success">
              <Check aria-hidden className="h-4 w-4" />
              승인
            </Button>
            <Button size="sm" variant="ghost">
              <X aria-hidden className="h-4 w-4" />
              취소
            </Button>
          </div>
        </li>
      ))}
    </ul>
  );
}
