"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Radio,
  Activity,
  History,
  RefreshCw,
  AlertTriangle,
  Network as NetworkIcon,
  Inbox as InboxIcon,
  Loader2,
} from "lucide-react";
import { StatCard } from "@/components/console/stat-card";
import { LiveFeed } from "@/components/console/live-feed";
import { WorkerAdmission } from "@/components/console/worker-admission";
import { ManagerQueue } from "@/components/console/manager-queue";
import { OrgSummary } from "@/components/console/org-summary";
import { feedEvents, pendingWorkers, type ConsoleMetric } from "@/lib/mock-data";
import {
  getMonitor,
  getOrgGraph,
  getManagerQueue,
  type AuditSummary,
  type OrgGraph,
  type ManagerItem,
} from "@/lib/console-api";
import { MockMarker } from "@/components/session/mock-marker";
import { useSession } from "@/components/session/session-context";
import type { StatusTone } from "@/components/ui/status-badge";
import { cn } from "@/lib/utils";

// Derive the metric strip from real audit records (web.py summarize_audit_record).
function deriveMetrics(records: AuditSummary[], orgCardCount: number): ConsoleMetric[] {
  const total = records.length;
  const routed = records.filter((r) => r.disposition === "routed").length;
  const contested = records.filter((r) => r.disposition === "contested").length;
  const unowned = records.filter((r) => r.disposition === "unowned").length;
  const routedPct = total > 0 ? Math.round((routed / total) * 100) : 0;
  return [
    {
      id: "m1",
      label: "총 라우팅 기록",
      value: String(total),
      unit: "건",
      tone: "info",
      hint: "감사 로그 누적(dedup)",
    },
    {
      id: "m2",
      label: "Routed",
      value: String(routedPct),
      unit: "%",
      tone: routedPct >= 70 ? "success" : "warning",
      hint: `담당 매칭 ${routed}건`,
    },
    {
      id: "m3",
      label: "Contested",
      value: String(contested),
      unit: "건",
      tone: contested > 0 ? "warning" : "neutral",
      hint: "담당 지정 필요",
    },
    {
      id: "m4",
      label: "Unowned",
      value: String(unowned),
      unit: "건",
      tone: unowned > 0 ? "warning" : "neutral",
      hint: `등록 카드 ${orgCardCount}종`,
    },
  ];
}

function dispositionTone(d: string | null): StatusTone {
  switch (d) {
    case "routed":
      return "success";
    case "contested":
      return "warning";
    case "unowned":
      return "danger";
    default:
      return "neutral";
  }
}

function dispositionLabel(d: string | null): string {
  switch (d) {
    case "routed":
      return "Routed";
    case "contested":
      return "Contested";
    case "unowned":
      return "Unowned";
    default:
      return d ?? "—";
  }
}

function shortTime(ts: string | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString("ko-KR", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return ts;
  }
}

export function ConsoleView() {
  const { userId } = useSession();
  const [records, setRecords] = useState<AuditSummary[]>([]);
  const [graph, setGraph] = useState<OrgGraph | null>(null);
  const [queue, setQueue] = useState<ManagerItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [m, g, q] = await Promise.all([
        getMonitor(),
        getOrgGraph(),
        getManagerQueue().catch(() => [] as ManagerItem[]),
      ]);
      setRecords(m);
      setGraph(g);
      setQueue(q);
    } catch (e) {
      setError(e instanceof Error ? e.message : "콘솔 데이터를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, userId]);

  const orgCardCount = useMemo(
    () => (graph?.nodes ?? []).filter((n) => n.type === "card").length,
    [graph],
  );
  const metrics = useMemo(
    () => deriveMetrics(records, orgCardCount),
    [records, orgCardCount],
  );
  // Newest-first for the recent-activity list.
  const recent = useMemo(() => [...records].reverse().slice(0, 12), [records]);

  return (
    <div className="flex flex-col gap-ds-16 px-ds-16 py-ds-16 md:px-ds-24">
      <div className="flex items-center justify-between gap-ds-8">
        <p className="text-xs text-[var(--ds-color-ink-subtle)]">
          {loading ? "감사 로그·조직 맵 불러오는 중…" : `감사 로그 ${records.length}건 · 조직 카드 ${orgCardCount}종`}
        </p>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex items-center gap-ds-4 rounded-pill border border-[var(--ds-color-border)] px-ds-12 py-[5px] text-xs font-medium text-[var(--ds-color-ink-muted)] transition-colors hover:bg-[var(--ds-color-surface-muted)] disabled:opacity-50"
        >
          <RefreshCw aria-hidden className={cn("h-[14px] w-[14px]", loading && "animate-ds-spin")} />
          새로고침
        </button>
      </div>

      {error && (
        <div
          role="alert"
          className="flex items-center gap-ds-8 rounded-md border border-[var(--ds-color-danger)] bg-[color-mix(in_srgb,var(--ds-color-danger)_8%,transparent)] px-ds-12 py-ds-8 text-sm text-[var(--ds-color-ink)]"
        >
          <AlertTriangle aria-hidden className="h-4 w-4 shrink-0 text-[var(--ds-color-danger)]" />
          {error}
        </div>
      )}

      {/* metric strip — derived from /monitor + /org/graph */}
      <div className="grid grid-cols-2 gap-ds-12 xl:grid-cols-4">
        {metrics.map((m) => (
          <StatCard key={m.id} metric={m} />
        ))}
      </div>

      <div className="grid grid-cols-1 gap-ds-16 lg:grid-cols-3">
        {/* primary: recent activity (real /monitor) */}
        <section className="overflow-hidden rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] lg:col-span-2">
          <div className="flex items-center gap-ds-8 border-b border-[var(--ds-color-border)] px-ds-16 py-ds-12">
            <Activity aria-hidden className="h-[18px] w-[18px] text-[var(--ds-color-info)]" />
            <h2 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
              최근 라우팅 활동
            </h2>
            <span className="ml-auto text-xs text-[var(--ds-color-ink-subtle)]">/monitor</span>
          </div>
          {loading && records.length === 0 ? (
            <PanelLoading label="감사 로그" />
          ) : recent.length === 0 ? (
            <PanelEmpty icon={InboxIcon} label="기록된 라우팅 활동이 없습니다." />
          ) : (
            <ul className="ds-scrollbar-thin flex max-h-[460px] flex-col divide-y divide-[var(--ds-color-border)] overflow-y-auto">
              {recent.map((r) => (
                <li key={r.index} className="flex items-start gap-ds-8 px-ds-16 py-ds-12">
                  <span
                    aria-hidden
                    className="mt-[6px] h-2 w-2 shrink-0 rounded-pill"
                    style={{
                      background: `var(--ds-color-${
                        dispositionTone(r.disposition) === "success"
                          ? "success"
                          : dispositionTone(r.disposition) === "warning"
                            ? "warning"
                            : dispositionTone(r.disposition) === "danger"
                              ? "danger"
                              : "ink-subtle"
                      })`,
                    }}
                  />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm text-[var(--ds-color-ink)]">
                      {r.question || "(빈 질문)"}
                    </p>
                    <div className="mt-ds-4 flex flex-wrap items-center gap-ds-8">
                      <span
                        className="rounded-sm px-ds-8 py-[1px] text-xs font-medium"
                        style={{
                          background: `color-mix(in srgb, var(--ds-color-${
                            dispositionTone(r.disposition) === "success"
                              ? "success"
                              : dispositionTone(r.disposition) === "warning"
                                ? "warning"
                                : dispositionTone(r.disposition) === "danger"
                                  ? "danger"
                                  : "ink-subtle"
                          }) 16%, transparent)`,
                          color: `var(--ds-color-${
                            dispositionTone(r.disposition) === "success"
                              ? "success"
                              : dispositionTone(r.disposition) === "warning"
                                ? "warning"
                                : dispositionTone(r.disposition) === "danger"
                                  ? "danger"
                                  : "ink-muted"
                          })`,
                        }}
                      >
                        {dispositionLabel(r.disposition)}
                      </span>
                      {r.intent && (
                        <span className="text-xs text-[var(--ds-color-ink-subtle)]">
                          intent: {r.intent}
                        </span>
                      )}
                      {r.mode && (
                        <span className="text-xs text-[var(--ds-color-ink-subtle)]">
                          {r.mode}
                        </span>
                      )}
                      <span className="ml-auto font-mono text-xs text-[var(--ds-color-ink-subtle)]">
                        {shortTime(r.timestamp)}
                      </span>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* side rail */}
        <aside className="flex flex-col gap-ds-16">
          {/* manager queue (real /manager/queue) */}
          <section className="overflow-hidden rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)]">
            <div className="flex items-center gap-ds-8 border-b border-[var(--ds-color-border)] px-ds-16 py-ds-12">
              <History aria-hidden className="h-[18px] w-[18px] text-[var(--ds-color-warning)]" />
              <h2 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
                매니저 큐
              </h2>
              <span className="ml-auto text-xs text-[var(--ds-color-ink-subtle)]">/manager/queue</span>
            </div>
            <ManagerQueue items={queue} loading={loading} />
          </section>

          {/* org map summary (real /org/graph) */}
          <section className="overflow-hidden rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)]">
            <div className="flex items-center gap-ds-8 border-b border-[var(--ds-color-border)] px-ds-16 py-ds-12">
              <NetworkIcon aria-hidden className="h-[18px] w-[18px] text-[var(--ds-color-primary)]" />
              <h2 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
                조직 맵
              </h2>
              <span className="ml-auto text-xs text-[var(--ds-color-ink-subtle)]">/org/graph</span>
            </div>
            <OrgSummary graph={graph} loading={loading} />
          </section>
        </aside>
      </div>

      {/* mock surfaces — routes not yet implemented backend-side */}
      <div className="grid grid-cols-1 gap-ds-16 lg:grid-cols-3">
        <section className="overflow-hidden rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] lg:col-span-2">
          <div className="flex items-center gap-ds-8 border-b border-[var(--ds-color-border)] px-ds-16 py-ds-12">
            <Activity aria-hidden className="h-[18px] w-[18px] text-[var(--ds-color-info)]" />
            <h2 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
              라이브 피드
            </h2>
          </div>
          <div className="px-ds-16 pt-ds-12">
            <MockMarker note="SSE 라이브 피드 라우트가 백엔드에 아직 없습니다 (SSE 피드 미구현). 실시간성은 위 '최근 라우팅 활동'을 새로고침으로 대체합니다." />
          </div>
          <LiveFeed events={feedEvents} />
        </section>

        <aside className="flex flex-col gap-ds-16">
          <section className="overflow-hidden rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)]">
            <div className="flex items-center gap-ds-8 border-b border-[var(--ds-color-border)] px-ds-16 py-ds-12">
              <Radio aria-hidden className="h-[18px] w-[18px] text-[var(--ds-color-warning)]" />
              <h2 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
                워커 승인 대기
              </h2>
            </div>
            <div className="px-ds-16 pt-ds-12">
              <MockMarker note="워커 admission 라우트가 백엔드에 아직 없습니다 (워커 admission 미구현)." />
            </div>
            <WorkerAdmission workers={pendingWorkers} />
          </section>
        </aside>
      </div>
    </div>
  );
}

function PanelLoading({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-ds-8 px-ds-16 py-ds-16 text-sm text-[var(--ds-color-ink-subtle)]">
      <Loader2 aria-hidden className="h-4 w-4 animate-ds-spin" />
      {label} 불러오는 중…
    </div>
  );
}

function PanelEmpty({
  icon: Icon,
  label,
}: {
  icon: typeof InboxIcon;
  label: string;
}) {
  return (
    <div className="flex flex-col items-center gap-ds-8 px-ds-16 py-ds-24 text-center">
      <Icon aria-hidden className="h-6 w-6 text-[var(--ds-color-ink-subtle)]" />
      <p className="text-sm text-[var(--ds-color-ink-muted)]">{label}</p>
    </div>
  );
}
