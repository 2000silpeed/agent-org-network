import { Loader2, User, Bot } from "lucide-react";
import type { OrgGraph } from "@/lib/console-api";
import { Tag } from "@/components/ui/tag";

// Org map summary (web.py serialize_org_graph). Projects the registry {nodes,
// edges} into a compact owner→card list. Operational surface — domains exposed OK.
export function OrgSummary({
  graph,
  loading,
}: {
  graph: OrgGraph | null;
  loading: boolean;
}) {
  if (loading && graph == null) {
    return (
      <div className="flex items-center gap-ds-8 px-ds-16 py-ds-16 text-sm text-[var(--ds-color-ink-subtle)]">
        <Loader2 aria-hidden className="h-4 w-4 animate-ds-spin" />
        조직 맵 불러오는 중…
      </div>
    );
  }
  if (graph == null) {
    return (
      <div className="px-ds-16 py-ds-16 text-sm text-[var(--ds-color-ink-subtle)]">
        조직 맵을 불러오지 못했습니다.
      </div>
    );
  }

  const users = graph.nodes.filter((n) => n.type === "user");
  const cards = graph.nodes.filter((n) => n.type === "card");
  const managerCount = users.filter((u) => u.manager == null).length;

  return (
    <div className="flex flex-col">
      <div className="flex items-center gap-ds-16 border-b border-[var(--ds-color-border)] px-ds-16 py-ds-12 text-xs text-[var(--ds-color-ink-muted)]">
        <span className="inline-flex items-center gap-ds-4">
          <User aria-hidden className="h-[14px] w-[14px] text-[var(--ds-color-ink-subtle)]" />
          User {users.length}
        </span>
        <span className="inline-flex items-center gap-ds-4">
          <Bot aria-hidden className="h-[14px] w-[14px] text-[var(--ds-color-ink-subtle)]" />
          Card {cards.length}
        </span>
        <span className="text-[var(--ds-color-ink-subtle)]">루트 매니저 {managerCount}</span>
      </div>
      <ul className="ds-scrollbar-thin flex max-h-[260px] flex-col divide-y divide-[var(--ds-color-border)] overflow-y-auto">
        {cards.map((card) => (
          <li key={card.agent_id} className="flex flex-col gap-ds-4 px-ds-16 py-ds-8">
            <div className="flex flex-wrap items-center gap-ds-8">
              <Bot aria-hidden className="h-4 w-4 shrink-0 text-[var(--ds-color-primary)]" />
              <span className="text-sm font-medium text-[var(--ds-color-ink)]">
                {card.agent_id}
              </span>
              <span className="text-xs text-[var(--ds-color-ink-subtle)]">
                owner: {card.owner} · {card.team}
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-ds-4 pl-6">
              {(card.domains ?? []).map((d) => (
                <Tag key={d} tone="neutral">
                  {d}
                </Tag>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
