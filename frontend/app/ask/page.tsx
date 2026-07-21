"use client";

import { useEffect, useRef, useState } from "react";
import Image from "next/image";
import { Send, UserRound, Network, AlertCircle, Loader2 } from "lucide-react";
import { PageHeader } from "@/components/app-shell/page-header";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { Tag } from "@/components/ui/tag";
import { RoutingTrace } from "@/components/ask/routing-trace";
import { SourceCard } from "@/components/ask/source-card";
import type { RoutingStep, SourceCard as SourceCardData } from "@/lib/mock-data";
import {
  streamAsk,
  getRequest,
  modeMeta,
  reviewStatusLabel,
  pendingTraceLabel,
  pendingUserMessage,
  AskError,
  type AnswerMode,
  type AskDone,
  type ReviewStatus,
} from "@/lib/ask-api";

type UserTurn = { id: string; role: "user"; text: string };

type OrgTurn = {
  id: string;
  role: "org";
  requestId?: string;
  recordId?: string;
  // answered
  text?: string;
  owner?: string;
  agentId?: string;
  mode?: AnswerMode;
  reviewStatus?: ReviewStatus;
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
      let completed: AskDone | undefined;
      await streamAsk(q, {
        onAccepted: (event) => {
          patchTurn(orgId, (t) => {
            t.requestId = event.request_id;
            t.trace = [
              { id: "tr1", label: "질문 접수됨", state: "done" },
              { id: "tr2", label: "담당 확인 중", state: "active" },
            ];
          });
        },
        onToken: (event) => {
          patchTurn(orgId, (t) => {
            t.loading = false;
            t.text = (t.text ?? "") + event.text;
          });
        },
        onDone: (event) => {
          completed = event;
          patchTurn(orgId, (t) => {
            t.requestId = event.request_id;
            t.recordId = event.record_id;
            t.loading = true;
            t.mode = event.mode;
            t.reviewStatus = event.review_status;
            t.trace = [
              { id: "tr1", label: "질문 접수됨", state: "done" },
              { id: "tr2", label: "답변 확정됨", state: "done" },
              { id: "tr3", label: "확정된 답 불러오는 중", state: "active" },
            ];
          });
        },
        onPending: (event) => {
          patchTurn(orgId, (t) => {
            t.requestId = event.request_id;
            t.loading = false;
            t.text = undefined;
            t.pendingMessage = pendingUserMessage(event);
            t.trace = [
              { id: "tr1", label: "질문 접수됨", state: "done" },
              { id: "tr2", label: pendingTraceLabel(event.kind), state: "done" },
            ];
          });
        },
        onDeclined: (event) => {
          patchTurn(orgId, (t) => {
            t.requestId = event.request_id;
            t.loading = false;
            t.text = undefined;
            t.error = "질문 처리가 거절되었습니다.";
            t.trace = undefined;
          });
        },
        onFailed: (event) => {
          patchTurn(orgId, (t) => {
            t.requestId = event.request_id;
            t.loading = false;
            t.text = undefined;
            t.error = "질문을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.";
            t.trace = undefined;
          });
        },
        onInterrupted: (event) => {
          patchTurn(orgId, (t) => {
            t.requestId = event.request_id;
            t.loading = false;
            t.text = undefined;
            t.error = event.retryable
              ? "처리가 잠시 중단되었습니다. 같은 요청 ID로 다시 확인해 주세요."
              : "처리를 이어갈 수 없습니다. 새 질문으로 다시 시도해 주세요.";
            t.trace = undefined;
          });
        },
      });

      const done = completed;
      if (done) {
        const canonical = await getRequest(done.request_id);
        if (
          canonical?.type !== "answered" ||
          canonical.record_id !== done.record_id ||
          canonical.mode !== done.mode ||
          canonical.review_status !== done.review_status ||
          canonical.answered_by.owner !== done.answered_by ||
          canonical.answered_by.agent_id !== done.agent_id ||
          canonical.sources.length !== done.sources.length ||
          canonical.sources.some((source, index) => source !== done.sources[index])
        ) {
          throw new AskError("확정된 답변을 안전하게 불러오지 못했습니다.");
        }
        patchTurn(orgId, (t) => {
          t.requestId = canonical.request_id;
          t.recordId = canonical.record_id;
          t.loading = false;
          t.text = canonical.text;
          t.owner = canonical.answered_by.owner;
          t.agentId = canonical.answered_by.agent_id;
          t.mode = canonical.mode;
          t.reviewStatus = canonical.review_status;
          t.sources = toSourceCards(
            canonical.sources,
            canonical.answered_by.owner,
            canonical.answered_by.agent_id
          );
          t.trace = [
            { id: "tr1", label: "질문 접수됨", state: "done" },
            { id: "tr2", label: "답변 확정됨", state: "done" },
            { id: "tr3", label: "확정된 답 확인 완료", state: "done" },
          ];
        });
      }
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
    <div className="flex h-full min-h-[calc(100vh-0px)] flex-col lg:min-h-0">
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
            <div className="mx-auto mt-ds-24 flex max-w-md flex-col items-center text-center">
              <Image
                src="/brand/empty-ask.png"
                alt=""
                width={420}
                height={238}
                priority
                className="ds-img-dark mb-ds-16 w-full max-w-[360px] opacity-90"
              />
              <Image
                src="/brand/empty-ask-light.png"
                alt=""
                width={420}
                height={238}
                priority
                className="ds-img-light mb-ds-16 w-full max-w-[360px]"
              />
              <p className="text-sm text-[var(--ds-color-ink-subtle)]">
                조직에 궁금한 것을 물어보세요. 질문은 담당을 찾아 흘러갑니다. 예:
                &ldquo;환불 규정 어떻게 돼?&rdquo;, &ldquo;계약 검토 어떻게 받아?&rdquo;
              </p>
            </div>
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
                      <>
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
                        {turn.requestId && (
                          <div className="mt-ds-8 flex flex-wrap items-center gap-ds-8">
                            <Tag tone="neutral">요청 {turn.requestId}</Tag>
                          </div>
                        )}
                      </>
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

                        {/* Request ID와 확정된 책임·신뢰 표식만 표시한다. */}
                        {(turn.requestId || turn.owner || turn.mode || turn.reviewStatus) && (
                          <div className="mt-ds-8 flex flex-wrap items-center gap-ds-8">
                            {turn.requestId && (
                              <Tag tone="neutral">요청 {turn.requestId}</Tag>
                            )}
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
                            {turn.mode && (
                              <StatusBadge
                                tone={modeMeta[turn.mode].tone}
                                label={modeMeta[turn.mode].label}
                              />
                            )}
                            {turn.reviewStatus && (
                              <Tag tone="success">
                                {reviewStatusLabel[turn.reviewStatus]}
                              </Tag>
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
