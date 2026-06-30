import { Radio, Activity, History } from "lucide-react";
import { PageHeader } from "@/components/app-shell/page-header";
import { StatCard } from "@/components/console/stat-card";
import { LiveFeed } from "@/components/console/live-feed";
import { WorkerAdmission } from "@/components/console/worker-admission";
import { AuditTimeline } from "@/components/console/audit-timeline";
import {
  consoleMetrics,
  feedEvents,
  pendingWorkers,
  auditEntries,
} from "@/lib/mock-data";

export default function ConsolePage() {
  return (
    <div className="flex flex-col">
      <PageHeader
        surface="Console"
        persona="운영자"
        title="운영 콘솔"
        description="라우팅·답변·워커 이벤트를 실시간으로 관찰하고, 워커 승인과 감사 로그를 한 화면에서 처리합니다."
        actions={
          <span className="inline-flex items-center gap-ds-8 rounded-pill border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-12 py-[5px] text-xs font-medium text-[var(--ds-color-ink-muted)]">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ds-pulse rounded-pill bg-[var(--ds-color-success)]" />
              <span className="relative inline-flex h-2 w-2 rounded-pill bg-[var(--ds-color-success)]" />
            </span>
            SSE 연결됨
          </span>
        }
      />

      <div className="flex flex-col gap-ds-16 px-ds-16 py-ds-16 md:px-ds-24">
        {/* metric strip */}
        <div className="grid grid-cols-2 gap-ds-12 xl:grid-cols-4">
          {consoleMetrics.map((m) => (
            <StatCard key={m.id} metric={m} />
          ))}
        </div>

        <div className="grid grid-cols-1 gap-ds-16 lg:grid-cols-3">
          {/* live feed — primary operational surface */}
          <section className="lg:col-span-2 overflow-hidden rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)]">
            <div className="flex items-center gap-ds-8 border-b border-[var(--ds-color-border)] px-ds-16 py-ds-12">
              <Activity
                aria-hidden
                className="h-[18px] w-[18px] text-[var(--ds-color-info)]"
              />
              <h2 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
                라이브 피드
              </h2>
            </div>
            <LiveFeed events={feedEvents} />
          </section>

          {/* side rail: worker admission + audit */}
          <aside className="flex flex-col gap-ds-16">
            <section className="overflow-hidden rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)]">
              <div className="flex items-center gap-ds-8 border-b border-[var(--ds-color-border)] px-ds-16 py-ds-12">
                <Radio
                  aria-hidden
                  className="h-[18px] w-[18px] text-[var(--ds-color-warning)]"
                />
                <h2 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
                  워커 승인 대기
                </h2>
              </div>
              <WorkerAdmission workers={pendingWorkers} />
            </section>

            <section className="overflow-hidden rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)]">
              <div className="flex items-center gap-ds-8 border-b border-[var(--ds-color-border)] px-ds-16 py-ds-12">
                <History
                  aria-hidden
                  className="h-[18px] w-[18px] text-[var(--ds-color-ink-subtle)]"
                />
                <h2 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
                  감사 타임라인
                </h2>
              </div>
              <AuditTimeline entries={auditEntries} />
            </section>
          </aside>
        </div>
      </div>
    </div>
  );
}
