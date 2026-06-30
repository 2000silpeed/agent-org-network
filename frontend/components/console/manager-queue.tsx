import { Loader2, Inbox as InboxIcon } from "lucide-react";
import type { ManagerItem } from "@/lib/console-api";
import { StatusBadge } from "@/components/ui/status-badge";

// Manager escalation queue (web.py serialize_manager_item). Source is a tagged
// union: from_unowned (0매칭 → 루트 escalation) · from_deadlock (다툼 교착) ·
// from_dispatch (디스패치 실패). Operational surface — internal values OK.
const SOURCE_META: Record<
  ManagerItem["source"]["type"],
  { label: string; tone: "danger" | "warning" | "info" }
> = {
  from_unowned: { label: "Unowned 에스컬레이션", tone: "danger" },
  from_deadlock: { label: "다툼 교착", tone: "warning" },
  from_dispatch: { label: "디스패치 실패", tone: "info" },
};

function sourceQuestion(s: ManagerItem["source"]): string {
  return s.question;
}

export function ManagerQueue({
  items,
  loading,
}: {
  items: ManagerItem[];
  loading: boolean;
}) {
  if (loading && items.length === 0) {
    return (
      <div className="flex items-center gap-ds-8 px-ds-16 py-ds-16 text-sm text-[var(--ds-color-ink-subtle)]">
        <Loader2 aria-hidden className="h-4 w-4 animate-ds-spin" />
        매니저 큐 불러오는 중…
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <div className="flex flex-col items-center gap-ds-8 px-ds-16 py-ds-24 text-center">
        <InboxIcon aria-hidden className="h-6 w-6 text-[var(--ds-color-ink-subtle)]" />
        <p className="text-sm text-[var(--ds-color-ink-muted)]">
          에스컬레이션 대기 항목이 없습니다.
        </p>
        <p className="text-xs text-[var(--ds-color-ink-subtle)]">
          (이 데모 구성에서는 매니저 큐 store가 연결되어 있지 않습니다.)
        </p>
      </div>
    );
  }
  return (
    <ul className="flex flex-col divide-y divide-[var(--ds-color-border)]">
      {items.map((it) => {
        const meta = SOURCE_META[it.source.type];
        return (
          <li key={it.item_id} className="flex flex-col gap-ds-4 px-ds-16 py-ds-12">
            <div className="flex flex-wrap items-center gap-ds-8">
              <StatusBadge tone={meta.tone} label={meta.label} />
              <span className="text-xs text-[var(--ds-color-ink-subtle)]">{it.status}</span>
            </div>
            <p className="text-sm text-[var(--ds-color-ink)]">{sourceQuestion(it.source)}</p>
          </li>
        );
      })}
    </ul>
  );
}
