"use client";

import { useState } from "react";
import {
  GitMerge,
  UserCheck,
  RefreshCw,
  FileSearch,
  Check,
  Pencil,
  X,
  Clock,
  ArrowRightLeft,
} from "lucide-react";
import {
  contestedCases,
  backupAnswers,
  reevaluationCases,
} from "@/lib/mock-data";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/card";
import { Tag } from "@/components/ui/tag";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type TabId = "contested" | "backup" | "reeval";

const tabs: { id: TabId; label: string; icon: typeof GitMerge; count: number }[] =
  [
    {
      id: "contested",
      label: "다툼",
      icon: GitMerge,
      count: contestedCases.length,
    },
    {
      id: "backup",
      label: "백업 답",
      icon: UserCheck,
      count: backupAnswers.length,
    },
    {
      id: "reeval",
      label: "재평가",
      icon: RefreshCw,
      count: reevaluationCases.length,
    },
  ];

export function InboxTabs() {
  const [active, setActive] = useState<TabId>("contested");

  return (
    <div className="px-ds-16 py-ds-16 md:px-ds-24">
      {/* saved-view-bar style tab list */}
      <div
        role="tablist"
        aria-label="처리함 탭"
        className="ds-scrollbar-thin mb-ds-16 flex gap-ds-8 overflow-x-auto border-b border-[var(--ds-color-border)]"
      >
        {tabs.map((t) => {
          const Icon = t.icon;
          const selected = active === t.id;
          return (
            <button
              key={t.id}
              role="tab"
              aria-selected={selected}
              onClick={() => setActive(t.id)}
              className={cn(
                "-mb-px inline-flex shrink-0 items-center gap-ds-8 border-b-2 px-ds-12 py-ds-8 text-sm font-medium transition-colors duration-ds-fast",
                selected
                  ? "border-[var(--ds-color-primary)] text-[var(--ds-color-ink)]"
                  : "border-transparent text-[var(--ds-color-ink-subtle)] hover:text-[var(--ds-color-ink-muted)]"
              )}
            >
              <Icon aria-hidden className="h-4 w-4" />
              {t.label}
              <span className="rounded-pill bg-[var(--ds-color-surface-muted)] px-ds-8 py-[1px] text-xs text-[var(--ds-color-ink-muted)]">
                {t.count}
              </span>
            </button>
          );
        })}
      </div>

      {active === "contested" && <ContestedPanel />}
      {active === "backup" && <BackupPanel />}
      {active === "reeval" && <ReevalPanel />}
    </div>
  );
}

function ContestedPanel() {
  return (
    <div
      role="tabpanel"
      className="flex flex-col gap-ds-16"
      aria-label="다툼 케이스"
    >
      {contestedCases.map((c) => (
        <Card key={c.id} elevated>
          <CardHeader>
            <div className="flex items-center gap-ds-8">
              <StatusBadge tone="warning" label="담당 미정" />
              <span className="text-xs text-[var(--ds-color-ink-subtle)]">
                <Clock aria-hidden className="mr-ds-2 inline h-3 w-3" />
                {c.askedAt}
              </span>
            </div>
            <p className="text-md font-medium text-[var(--ds-color-ink)]">
              {c.question}
            </p>
          </CardHeader>
          <CardBody className="flex flex-col gap-ds-12">
            <div className="flex flex-wrap items-center gap-ds-4">
              <span className="text-xs text-[var(--ds-color-ink-subtle)]">
                질문-연관 개념:
              </span>
              {c.relatedConcepts.map((rc) => (
                <Tag key={rc} tone="neutral">
                  {rc}
                </Tag>
              ))}
            </div>

            <div className="grid grid-cols-1 gap-ds-12 md:grid-cols-2">
              {c.candidates.map((cand) => (
                <div
                  key={cand.owner}
                  className="rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-12"
                >
                  <div className="flex flex-wrap items-center justify-between gap-ds-8">
                    <span className="font-medium text-[var(--ds-color-ink)]">
                      {cand.owner}
                      <span className="ml-ds-4 text-xs font-normal text-[var(--ds-color-ink-subtle)]">
                        {cand.domain}
                      </span>
                    </span>
                    <StatusBadge
                      tone={cand.coverageTone}
                      label={cand.coverageLabel}
                    />
                  </div>
                  <div className="mt-ds-8 flex flex-wrap items-center gap-ds-4">
                    {cand.matchedConcepts.map((mc) => (
                      <Tag key={mc} tone="info">
                        {mc}
                      </Tag>
                    ))}
                  </div>
                  <button className="mt-ds-8 inline-flex items-center gap-ds-4 text-xs font-medium text-[var(--ds-color-link)] hover:underline">
                    <FileSearch aria-hidden className="h-[13px] w-[13px]" />
                    문서 on-demand 추출
                  </button>
                </div>
              ))}
            </div>
          </CardBody>
          <CardFooter>
            <span className="mr-auto text-xs text-[var(--ds-color-ink-subtle)]">
              커버리지를 보고 담당을 지정하세요.
            </span>
            {c.candidates.map((cand) => (
              <Button key={cand.owner} size="sm" variant="secondary">
                <ArrowRightLeft aria-hidden className="h-4 w-4" />
                {cand.owner} 지정
              </Button>
            ))}
          </CardFooter>
        </Card>
      ))}
    </div>
  );
}

function BackupPanel() {
  return (
    <div
      role="tabpanel"
      className="flex flex-col gap-ds-12"
      aria-label="백업 답변"
    >
      {backupAnswers.map((b) => (
        <Card key={b.id} elevated>
          <CardHeader>
            <div className="flex flex-wrap items-center gap-ds-8">
              <StatusBadge tone="info" label="백업 답변" />
              <span className="text-xs text-[var(--ds-color-ink-subtle)]">
                {b.answeredBy} 작성 · {b.absentOwner} 부재 · {b.answeredAt}
              </span>
            </div>
            <p className="text-md font-medium text-[var(--ds-color-ink)]">
              {b.question}
            </p>
          </CardHeader>
          <CardBody>
            <p className="text-sm leading-normal text-[var(--ds-color-ink-muted)]">
              {b.answerPreview}
            </p>
          </CardBody>
          <CardFooter>
            <Button size="sm" variant="success">
              <Check aria-hidden className="h-4 w-4" />
              승인
            </Button>
            <Button size="sm" variant="secondary">
              <Pencil aria-hidden className="h-4 w-4" />
              정정
            </Button>
            <Button size="sm" variant="ghost">
              <X aria-hidden className="h-4 w-4" />
              무시
            </Button>
          </CardFooter>
        </Card>
      ))}
    </div>
  );
}

function ReevalPanel() {
  return (
    <div
      role="tabpanel"
      className="flex flex-col gap-ds-12"
      aria-label="재평가 케이스"
    >
      {reevaluationCases.map((r) => (
        <Card key={r.id} elevated>
          <CardHeader>
            <div className="flex flex-wrap items-center gap-ds-8">
              <StatusBadge tone="warning" label="stale · 재평가 필요" />
              <span className="text-xs text-[var(--ds-color-ink-subtle)]">
                {r.pastAnsweredAt}
              </span>
            </div>
            <p className="text-md font-medium text-[var(--ds-color-ink)]">
              {r.question}
            </p>
          </CardHeader>
          <CardBody className="flex flex-col gap-ds-8">
            <div className="flex flex-wrap items-center gap-ds-4 text-xs text-[var(--ds-color-ink-muted)]">
              <span>변경 개념:</span>
              <Tag tone="neutral">{r.changedConcept}</Tag>
            </div>
            <p className="text-sm text-[var(--ds-color-ink-muted)]">
              {r.reason}
            </p>
          </CardBody>
          <CardFooter>
            <Button size="sm" variant="secondary">
              <Check aria-hidden className="h-4 w-4" />
              유지
            </Button>
            <Button size="sm" variant="primary">
              <RefreshCw aria-hidden className="h-4 w-4" />
              재답변
            </Button>
            <Button size="sm" variant="danger">
              <X aria-hidden className="h-4 w-4" />
              무효화
            </Button>
          </CardFooter>
        </Card>
      ))}
    </div>
  );
}
