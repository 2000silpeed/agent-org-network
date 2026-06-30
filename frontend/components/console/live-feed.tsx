import {
  GitMerge,
  CheckCircle2,
  Plug,
  AlertTriangle,
  type LucideIcon,
} from "lucide-react";
import type { FeedEvent, FeedKind } from "@/lib/mock-data";
import { Tag } from "@/components/ui/tag";

const kindMeta: Record<
  FeedKind,
  { icon: LucideIcon; color: string }
> = {
  contested: { icon: GitMerge, color: "var(--ds-color-warning)" },
  answered: { icon: CheckCircle2, color: "var(--ds-color-success)" },
  worker: { icon: Plug, color: "var(--ds-color-info)" },
  escalation: { icon: AlertTriangle, color: "var(--ds-color-danger)" },
};

// activity-card / live feed: time + icon + text + tags. Single scroll surface,
// not a card wall. Icon reinforces the event kind alongside text.
export function LiveFeed({ events }: { events: FeedEvent[] }) {
  return (
    <ul className="ds-scrollbar-thin flex flex-col divide-y divide-[var(--ds-color-border)]">
      {events.map((e) => {
        const meta = kindMeta[e.kind];
        const Icon = meta.icon;
        return (
          <li key={e.id} className="flex items-start gap-ds-8 px-ds-16 py-ds-12">
            <Icon
              aria-hidden
              className="mt-[2px] h-[18px] w-[18px] shrink-0"
              style={{ color: meta.color }}
            />
            <div className="min-w-0 flex-1">
              <p className="text-sm text-[var(--ds-color-ink)]">{e.text}</p>
              <div className="mt-ds-4 flex flex-wrap items-center gap-ds-4">
                <span className="font-mono text-xs text-[var(--ds-color-ink-subtle)]">
                  {e.time}
                </span>
                {e.tags.map((t) => (
                  <Tag key={t} tone="neutral">
                    {t}
                  </Tag>
                ))}
              </div>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
