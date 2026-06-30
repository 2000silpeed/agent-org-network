"use client";

import { useEffect, useRef, useState } from "react";
import { Send, UserRound, Network, AlertCircle, Loader2 } from "lucide-react";
import { PageHeader } from "@/components/app-shell/page-header";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { Tag } from "@/components/ui/tag";
import { RoutingTrace } from "@/components/ask/routing-trace";
import { SourceCard } from "@/components/ask/source-card";
import type { RoutingStep, SourceCard as SourceCardData } from "@/lib/mock-data";
import { streamAsk, modeMeta, pendingTraceLabel, AskError } from "@/lib/ask-api";

type UserTurn = { id: string; role: "user"; text: string };

type OrgTurn = {
  id: string;
  role: "org";
  // answered
  text?: string;
  owner?: string;
  agentId?: string;
  confidence?: keyof typeof modeMeta;
  sources?: SourceCardData[];
  // pending / error
  trace?: RoutingStep[];
  pendingMessage?: string;
  error?: string;
  // transient
  loading?: boolean;
};

type Turn = UserTurn | OrgTurn;

let seq = 0;
const nextId = () => `t${Date.now()}-${seq++}`;

// Backend `sources` is a flat string[] (e.g. "위키/환불정책"). The source-card
// shows which OKF concept grounds the answer; map each path onto the card shape
// using only what the backend exposes (the source label + the answering owner).
function toSourceCards(
  sources: string[],
  owner: string,
  ownerDomain: string
): SourceCardData[] {
  return sources.map((s, i) => {
    const slash = s.indexOf("/");
    const leaf = slash >= 0 ? s.slice(slash + 1) : s;
    return {
      id: `src-${i}`,
      conceptId: s,
      title: leaf || s,
      owner,
      domain: ownerDomain,
      updatedAt: "출처",
    };
  });
}

export default function AskPage() {
  const [draft, setDraft] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [turns]);

  function patchTurn(id: string, fn: (t: OrgTurn) => void) {
    setTurns((prev) =>
      prev.map((t) => {
        if (t.id !== id || t.role !== "org") return t;
        const copy = { ...t };
        fn(copy);
        return copy;
      })
    );
  }

  async function submit(question: string) {
    const q = question.trim();
    if (!q || busy) return;
    setBusy(true);

    const orgId = nextId();
    setTurns((prev) => [
      ...prev,
      { id: nextId(), role: "user", text: q },
      {
        id: orgId,
        role: "org",
        loading: true,
        trace: [{ id: "tr1", label: "담당 찾는 중", state: "active" }],
      },
    ]);

    try {
      // SSE token streaming (ADR 0031): meta(담당 즉시) → token*(델타 누적) →
      // done(최종 신뢰 배지). Pending(다툼/담당 없음/대기)은 단독 안내. error는 중립.
      await streamAsk(q, {
        onMeta: (m) => {
          patchTurn(orgId, (t) => {
            t.owner = m.answered_by.owner;
            t.agentId = m.answered_by.agent_id;
            t.confidence = m.mode;
            t.sources = toSourceCards(
              m.sources,
              m.answered_by.owner,
              m.answered_by.agent_id
            );
            t.trace = [
              { id: "tr1", label: "담당 찾는 중", state: "done" },
              { id: "tr2", label: `${m.answered_by.agent_id} 전달됨`, state: "done" },
              { id: "tr3", label: "답변 작성 중", state: "active" },
            ];
          });
        },
        onToken: (delta) => {
          patchTurn(orgId, (t) => {
            t.loading = false;
            t.text = (t.text ?? "") + delta;
          });
        },
        onDone: (d) => {
          patchTurn(orgId, (t) => {
            t.loading = false;
            t.confidence = d.mode;
            if (d.sources.length > 0 && t.owner) {
              t.sources = toSourceCards(d.sources, t.owner, t.agentId ?? t.owner);
            }
            t.trace = [
              { id: "tr1", label: "담당 찾는 중", state: "done" },
              { id: "tr2", label: `${t.agentId ?? "담당"} 전달됨`, state: "done" },
              { id: "tr3", label: "답변 작성 완료", state: "done" },
            ];
          });
        },
        onPending: (p) => {
          patchTurn(orgId, (t) => {
            t.loading = false;
            t.pendingMessage = p.message;
            t.trace = [
              { id: "tr1", label: "담당 찾는 중", state: "done" },
              { id: "tr2", label: pendingTraceLabel(p.kind), state: "done" },
            ];
          });
        },
        onError: (msg) => {
          patchTurn(orgId, (t) => {
            t.loading = false;
            t.error = msg;
            t.trace = undefined;
          });
        },
      });
    } catch (err) {
      const msg =
        err instanceof AskError ? err.message : "알 수 없는 오류가 발생했습니다.";
      patchTurn(orgId, (t) => {
        t.loading = false;
        t.error = msg;
        t.trace = undefined;
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full min-h-[calc(100vh-0px)] flex-col">
      <PageHeader
        surface="Ask"
        persona="사용자"
        title="질문하기"
        description="질문을 보내면 조직이 담당을 찾아 답합니다. 답변에는 담당·신뢰 상태·출처가 함께 표시됩니다."
      />

      <div
        ref={scrollRef}
        className="ds-scrollbar-thin flex-1 overflow-y-auto px-ds-16 py-ds-16 md:px-ds-24"
      >
        <div aria-label="대화" className="mx-auto flex max-w-3xl flex-col gap-ds-16">
          {turns.length === 0 && (
            <p className="mx-auto mt-ds-24 max-w-md text-center text-sm text-[var(--ds-color-ink-subtle)]">
              조직에 궁금한 것을 물어보세요. 예: &ldquo;환불 규정 어떻게 돼?&rdquo;,
              &ldquo;계약 검토 어떻게 받아?&rdquo;
            </p>
          )}

          {turns.map((turn) =>
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

                    {/* error bubble — real network/4xx/5xx state, not a mock */}
                    {turn.error ? (
                      <div
                        role="alert"
                        className="flex items-start gap-ds-8 rounded-lg rounded-tl-sm border border-[color-mix(in_srgb,var(--ds-color-danger)_40%,transparent)] bg-[color-mix(in_srgb,var(--ds-color-danger)_8%,transparent)] px-ds-16 py-ds-12 text-sm text-[var(--ds-color-ink)]"
                      >
                        <AlertCircle
                          aria-hidden
                          className="mt-[2px] h-4 w-4 shrink-0 text-[var(--ds-color-danger)]"
                        />
                        <span>{turn.error}</span>
                      </div>
                    ) : turn.loading && !turn.text ? (
                      <div className="inline-flex items-center gap-ds-8 rounded-lg rounded-tl-sm border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-16 py-ds-12 text-sm text-[var(--ds-color-ink-muted)]">
                        <Loader2
                          aria-hidden
                          className="h-4 w-4 shrink-0 animate-ds-spin text-[var(--ds-color-info)]"
                        />
                        <span>
                          {turn.pendingMessage ?? "조직이 답을 준비하고 있어요…"}
                        </span>
                      </div>
                    ) : (
                      <>
                        <div className="whitespace-pre-wrap rounded-lg rounded-tl-sm border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-16 py-ds-12 text-sm leading-normal text-[var(--ds-color-ink)]">
                          {turn.text ?? turn.pendingMessage}
                        </div>

                        {/* owner · confidence — the only routing-facing chips users see */}
                        {(turn.owner || turn.confidence) && (
                          <div className="mt-ds-8 flex flex-wrap items-center gap-ds-8">
                            {turn.owner && (
                              <Tag tone="info">
                                담당 {turn.owner}
                                {turn.agentId && (
                                  <span className="text-[var(--ds-color-ink-subtle)]">
                                    · {turn.agentId}
                                  </span>
                                )}
                              </Tag>
                            )}
                            {turn.confidence && (
                              <StatusBadge
                                tone={modeMeta[turn.confidence].tone}
                                label={modeMeta[turn.confidence].label}
                              />
                            )}
                          </div>
                        )}

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
                      </>
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
            const q = draft;
            setDraft("");
            void submit(q);
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
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                const q = draft;
                setDraft("");
                void submit(q);
              }
            }}
            placeholder="조직에 질문을 입력하세요"
            className="ds-scrollbar-thin max-h-32 min-h-[44px] min-w-0 flex-1 resize-none rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)] px-ds-12 py-ds-8 text-sm text-[var(--ds-color-ink)] placeholder:text-[var(--ds-color-ink-subtle)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ds-color-primary)]"
          />
          <Button
            type="submit"
            size="icon"
            aria-label="질문 보내기"
            loading={busy}
            disabled={busy || draft.trim().length === 0}
          >
            {!busy && <Send aria-hidden className="h-4 w-4" />}
          </Button>
        </form>
        <p className="mx-auto mt-ds-8 max-w-3xl text-xs text-[var(--ds-color-ink-subtle)]">
          실 라우팅으로 동작합니다. 라우팅 내부(점수·후보)는 사용자에게 표시되지 않습니다.
        </p>
      </div>
    </div>
  );
}
