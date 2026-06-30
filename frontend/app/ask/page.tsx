"use client";

import { useState } from "react";
import { Send, UserRound, Network } from "lucide-react";
import { PageHeader } from "@/components/app-shell/page-header";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { Tag } from "@/components/ui/tag";
import { RoutingTrace } from "@/components/ask/routing-trace";
import { SourceCard } from "@/components/ask/source-card";
import { askThread, confidenceMeta } from "@/lib/mock-data";

export default function AskPage() {
  const [draft, setDraft] = useState("");

  return (
    <div className="flex h-full min-h-[calc(100vh-0px)] flex-col">
      <PageHeader
        surface="Ask"
        persona="사용자"
        title="질문하기"
        description="질문을 보내면 조직이 담당을 찾아 답합니다. 답변에는 담당·신뢰 상태·출처가 함께 표시됩니다."
      />

      <div className="ds-scrollbar-thin flex-1 overflow-y-auto px-ds-16 py-ds-16 md:px-ds-24">
        <div
          aria-label="대화"
          className="mx-auto flex max-w-3xl flex-col gap-ds-16"
        >
          {askThread.map((turn) =>
            turn.role === "user" ? (
              <div key={turn.id} className="flex justify-end">
                <div className="flex max-w-[85%] items-start gap-ds-8">
                  <div className="rounded-lg rounded-tr-sm bg-[var(--ds-color-surface-tint)] px-ds-16 py-ds-12 text-sm text-[var(--ds-color-ink)]">
                    {turn.text}
                  </div>
                  <span className="mt-[2px] flex h-7 w-7 shrink-0 items-center justify-center rounded-pill bg-[var(--ds-color-surface-muted)]">
                    <UserRound
                      aria-hidden
                      className="h-4 w-4 text-[var(--ds-color-ink-muted)]"
                    />
                  </span>
                </div>
              </div>
            ) : (
              <div key={turn.id} className="flex justify-start">
                <div className="flex max-w-[92%] items-start gap-ds-8">
                  <span className="mt-[2px] flex h-7 w-7 shrink-0 items-center justify-center rounded-pill bg-[var(--ds-color-surface-tint)]">
                    <Network
                      aria-hidden
                      className="h-4 w-4 text-[var(--ds-color-primary)]"
                    />
                  </span>
                  <div className="min-w-0 flex-1">
                    {turn.trace && (
                      <div className="mb-ds-8">
                        <RoutingTrace steps={turn.trace} />
                      </div>
                    )}
                    <div className="rounded-lg rounded-tl-sm border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-16 py-ds-12 text-sm leading-normal text-[var(--ds-color-ink)]">
                      {turn.text}
                    </div>

                    {/* owner · confidence · source — the only routing-facing facts users see */}
                    <div className="mt-ds-8 flex flex-wrap items-center gap-ds-8">
                      {turn.owner && (
                        <Tag tone="info">
                          담당 {turn.owner}
                          <span className="text-[var(--ds-color-ink-subtle)]">
                            · {turn.ownerDomain}
                          </span>
                        </Tag>
                      )}
                      {turn.confidence && (
                        <StatusBadge
                          tone={confidenceMeta[turn.confidence].tone}
                          label={confidenceMeta[turn.confidence].label}
                        />
                      )}
                    </div>

                    {turn.sources && turn.sources.length > 0 && (
                      <div className="mt-ds-8 flex flex-col gap-ds-8">
                        <p className="text-xs font-medium text-[var(--ds-color-ink-subtle)]">
                          출처
                        </p>
                        {turn.sources.map((s) => (
                          <SourceCard key={s.id} source={s} />
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )
          )}
        </div>
      </div>

      {/* chat-input */}
      <div className="border-t border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-16 py-ds-12 md:px-ds-24">
        <form
          className="mx-auto flex max-w-3xl items-end gap-ds-8"
          onSubmit={(e) => {
            e.preventDefault();
            setDraft("");
          }}
        >
          <label htmlFor="ask-input" className="sr-only">
            질문 입력
          </label>
          <textarea
            id="ask-input"
            rows={1}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="조직에 질문을 입력하세요"
            className="ds-scrollbar-thin max-h-32 min-h-[44px] min-w-0 flex-1 resize-none rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)] px-ds-12 py-ds-8 text-sm text-[var(--ds-color-ink)] placeholder:text-[var(--ds-color-ink-subtle)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ds-color-primary)]"
          />
          <Button type="submit" size="icon" aria-label="질문 보내기">
            <Send aria-hidden className="h-4 w-4" />
          </Button>
        </form>
        <p className="mx-auto mt-ds-8 max-w-3xl text-xs text-[var(--ds-color-ink-subtle)]">
          정적 목업입니다. 라우팅 내부(점수·후보)는 사용자에게 표시되지 않습니다.
        </p>
      </div>
    </div>
  );
}
