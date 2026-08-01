"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, Loader2, Send } from "lucide-react";
import { PageHeader } from "@/components/app-shell/page-header";
import { Button } from "@/components/ui/button";
import { Tag } from "@/components/ui/tag";
import {
  QuestionClientError,
  createQuestion,
  lifecycleMessage,
  retrieveQuestion,
  subscribeQuestion,
  submitFeedback,
  type AnsweredProjection,
  type FeedbackInput,
  type QuestionProjection,
  type QuestionStreamEvent,
} from "@/lib/ask-api";

type SessionState = "loading" | "authenticated" | "anonymous" | "unavailable";
type Display = { requestId: string; question: string; projection?: QuestionProjection; progressText?: string; error?: string; feedbackSent?: boolean };

function newKey(): string {
  return `${Date.now().toString(36)}-${crypto.getRandomValues(new Uint32Array(2)).join("")}`;
}

function authenticatedSession(value: unknown): boolean {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const raw = value as Record<string, unknown>;
  return Object.keys(raw).length === 4 && raw.authenticated === true && typeof raw.registry_user_ref === "string" && raw.registry_user_ref.length > 0 && typeof raw.expires_at === "string" && Array.isArray(raw.actions) && raw.actions.length === 1 && raw.actions[0] === "session.read";
}

export default function AskPage(): JSX.Element {
  const [session, setSession] = useState<SessionState>("loading");
  const [draft, setDraft] = useState("");
  const [display, setDisplay] = useState<Display | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const stopRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    let active = true;
    void fetch("/api/auth/session", { credentials: "same-origin", cache: "no-store" }).then(async (response) => {
      if (!active) return;
      if (response.status === 401) { setSession("anonymous"); return; }
      if (!response.ok || !authenticatedSession(await response.json())) { setSession("unavailable"); return; }
      setSession("authenticated");
    }).catch(() => { if (active) setSession("unavailable"); });
    return () => { active = false; stopRef.current?.(); };
  }, []);

  function patch(requestId: string, update: (current: Display) => Display): void {
    setDisplay((current) => current?.requestId === requestId ? update(current) : current);
  }

  async function converge(requestId: string): Promise<void> {
    try {
      const projection = await retrieveQuestion(requestId);
      patch(requestId, (current) => ({ ...current, projection, progressText: undefined, error: undefined }));
      if (projection.type !== "pending") stopRef.current?.();
    } catch (error) {
      const message = error instanceof QuestionClientError ? error.message : "질문 상태를 안전하게 확인하지 못했습니다.";
      patch(requestId, (current) => ({ ...current, error: message, progressText: undefined }));
    }
  }

  function observe(requestId: string): void {
    stopRef.current?.();
    stopRef.current = subscribeQuestion(requestId, {
      onEvent: (event: QuestionStreamEvent) => {
        if (event.type === "accepted") return;
        if (event.type === "token") {
          patch(requestId, (current) => ({ ...current, progressText: `${current.progressText ?? ""}${event.text}` }));
          return;
        }
        if (event.type === "pending") {
          patch(requestId, (current) => ({ ...current, projection: event.event, progressText: undefined, error: undefined }));
          return;
        }
        if (event.type === "interrupted") {
          patch(requestId, (current) => ({ ...current, progressText: lifecycleMessage(event), error: event.retryable ? undefined : lifecycleMessage(event) }));
          void converge(requestId);
          return;
        }
        // done is only a notification: all terminal content is re-read from GET.
        void converge(requestId);
      },
      onReconnect: () => patch(requestId, (current) => ({ ...current, progressText: "연결을 다시 확인하고 있습니다. 질문 처리는 계속됩니다." })),
      onFault: (error) => {
        patch(requestId, (current) => ({ ...current, error: error.message, progressText: undefined }));
        if (error.retryable) void converge(requestId);
      },
    });
  }

  async function ask(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (submitting || !draft.trim()) return;
    setSubmitting(true);
    try {
      const received = await createQuestion(draft, newKey());
      const question = draft;
      setDraft("");
      setDisplay({ requestId: received.request_id, question, progressText: "질문을 접수했습니다. 담당을 찾고 있습니다." });
      observe(received.request_id);
    } catch (error) {
      const message = error instanceof QuestionClientError ? error.message : "질문을 접수하지 못했습니다.";
      setDisplay({ requestId: "", question: draft, error: message });
    } finally { setSubmitting(false); }
  }

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader surface="Ask" persona="Question User" title="질문하기" description="조직 SSO 세션으로 질문을 접수하고, 안전한 수명주기 상태와 확정 답변을 확인합니다." />
      <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-ds-16 px-ds-16 py-ds-16 md:px-ds-24">
        <div aria-live="polite" aria-atomic="true" className="sr-only">{display?.error ?? display?.progressText ?? ""}</div>
        {session === "loading" && <Status message="조직 SSO 세션을 확인하고 있습니다." />}
        {session === "anonymous" && <LoginRequired />}
        {session === "unavailable" && <ErrorState message="SSO 세션을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요." />}
        {session === "authenticated" && (
          <>
            {display ? <QuestionResult display={display} onFeedback={(input, key) => submitRequesterFeedback(display, input, key, setDisplay)} /> : <Status message="질문을 보내면 담당을 찾는 과정을 안전한 상태로 알려드립니다." />}
            <form className="mt-auto flex flex-col gap-ds-8 border-t border-[var(--ds-color-border)] pt-ds-16" onSubmit={(event) => void ask(event)}>
              <label htmlFor="question" className="text-sm font-medium text-[var(--ds-color-ink)]">질문</label>
              <textarea id="question" name="question" required rows={3} value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="조직에 질문을 입력하세요" className="ds-scrollbar-thin min-h-24 resize-y rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-12 py-ds-8 text-sm text-[var(--ds-color-ink)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ds-color-primary)]" aria-describedby="question-help" />
              <p id="question-help" className="text-xs text-[var(--ds-color-ink-subtle)]">Enter가 아닌 제출 버튼으로 보냅니다. 접수 실패 시 입력한 질문은 그대로 남습니다.</p>
              <div className="flex justify-end"><Button type="submit" disabled={submitting || !draft.trim()} loading={submitting}><Send aria-hidden className="mr-ds-4 h-4 w-4" />질문 보내기</Button></div>
            </form>
          </>
        )}
      </div>
    </div>
  );
}

function QuestionResult({ display, onFeedback }: { display: Display; onFeedback: (input: FeedbackInput, key: string) => Promise<void> }): JSX.Element {
  const answered = display.projection?.type === "answered" ? display.projection : null;
  return <section aria-label="질문 결과" className="flex flex-col gap-ds-12">
    <div className="self-end rounded-lg bg-[var(--ds-color-surface-tint)] px-ds-16 py-ds-12 text-sm text-[var(--ds-color-ink)]">{display.question}</div>
    <article className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-16">
      {display.error ? <ErrorState message={display.error} /> : answered ? <Answered answer={answered} /> : <Pending display={display} />}
      {display.requestId && <Tag tone="neutral">요청 {display.requestId}</Tag>}
    </article>
    {answered && <Feedback key={answered.record_id} answer={answered} submitted={display.feedbackSent === true} onSubmit={onFeedback} />}
  </section>;
}

function Pending({ display }: { display: Display }): JSX.Element {
  const message = display.projection ? lifecycleMessage(display.projection) : display.progressText ?? "질문 상태를 확인하고 있습니다.";
  return <div className="flex items-start gap-ds-8"><Loader2 aria-hidden className="mt-0.5 h-4 w-4 shrink-0 animate-ds-spin text-[var(--ds-color-info)]" /><p className="text-sm text-[var(--ds-color-ink-muted)]">{message}</p></div>;
}

function Answered({ answer }: { answer: AnsweredProjection }): JSX.Element {
  return <div className="flex flex-col gap-ds-12"><div className="flex items-center gap-ds-8 text-sm font-medium text-[var(--ds-color-success)]"><CheckCircle2 aria-hidden className="h-4 w-4" />답변 확정</div><p className="whitespace-pre-wrap text-sm leading-relaxed text-[var(--ds-color-ink)]">{answer.text}</p><div className="flex flex-wrap gap-ds-8 text-xs text-[var(--ds-color-ink-subtle)]"><Tag tone="info">담당 {answer.answered_by.owner} · {answer.answered_by.agent_id}</Tag><Tag tone="neutral">{answer.mode === "full" ? "전체 답변" : "백업 답변"}</Tag><Tag tone="success">{answer.review_status === "approved" ? "검토 완료" : "별도 검토 불필요"}</Tag></div>{answer.sources.length > 0 && <div><p className="mb-ds-4 text-xs font-medium text-[var(--ds-color-ink-subtle)]">출처</p><ul className="flex flex-col gap-ds-4">{answer.sources.map((source) => <li key={source} className="text-xs text-[var(--ds-color-ink-muted)]">{source}</li>)}</ul></div>}</div>;
}

function Feedback({ answer, submitted, onSubmit }: { answer: AnsweredProjection; submitted: boolean; onSubmit: (input: FeedbackInput, key: string) => Promise<void> }): JSX.Element {
  const [verdict, setVerdict] = useState<"good" | "bad">("good");
  const [comment, setComment] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const keyRef = useRef(newKey());
  async function send(event: FormEvent<HTMLFormElement>): Promise<void> { event.preventDefault(); if (pending || submitted) return; setPending(true); setError(""); try { await onSubmit({ record_id: answer.record_id, verdict, comment }, keyRef.current); } catch (reason) { setError(reason instanceof QuestionClientError ? reason.message : "피드백을 보내지 못했습니다."); } finally { setPending(false); } }
  return <form onSubmit={(event) => void send(event)} className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface-muted)] p-ds-16"><fieldset disabled={submitted || pending}><legend className="mb-ds-8 text-sm font-medium text-[var(--ds-color-ink)]">이 답변이 도움이 되었나요?</legend><div className="flex gap-ds-8"><Button type="button" variant={verdict === "good" ? "primary" : "secondary"} onClick={() => setVerdict("good")}>도움됨</Button><Button type="button" variant={verdict === "bad" ? "primary" : "secondary"} onClick={() => setVerdict("bad")}>아쉬움</Button></div><label htmlFor={`feedback-${answer.record_id}`} className="mt-ds-12 block text-xs text-[var(--ds-color-ink-muted)]">의견 (선택)</label><textarea id={`feedback-${answer.record_id}`} value={comment} onChange={(event) => setComment(event.target.value)} rows={3} className="mt-ds-4 w-full rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-8 py-ds-8 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ds-color-primary)]" /><div className="mt-ds-8 flex justify-end"><Button type="submit" loading={pending}>피드백 보내기</Button></div></fieldset>{submitted && <p role="status" className="mt-ds-8 text-sm text-[var(--ds-color-success)]">피드백을 기록했습니다.</p>}{error && <p role="alert" className="mt-ds-8 text-sm text-[var(--ds-color-danger)]">{error}</p>}</form>;
}

async function submitRequesterFeedback(display: Display, input: FeedbackInput, key: string, setDisplay: (update: (current: Display | null) => Display | null) => void): Promise<void> {
  await submitFeedback(display.requestId, input, key);
  setDisplay((current) => current?.requestId === display.requestId ? { ...current, feedbackSent: true } : current);
}

function Status({ message }: { message: string }): JSX.Element { return <div className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-16 text-sm text-[var(--ds-color-ink-muted)]">{message}</div>; }
function ErrorState({ message }: { message: string }): JSX.Element { return <div role="alert" className="flex gap-ds-8 rounded-lg border border-[color-mix(in_srgb,var(--ds-color-danger)_40%,transparent)] bg-[color-mix(in_srgb,var(--ds-color-danger)_8%,transparent)] p-ds-16 text-sm text-[var(--ds-color-ink)]"><AlertCircle aria-hidden className="h-4 w-4 shrink-0 text-[var(--ds-color-danger)]" />{message}</div>; }
function LoginRequired(): JSX.Element { return <div className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-16"><p className="mb-ds-8 text-sm text-[var(--ds-color-ink-muted)]">질문을 보내려면 조직 SSO 로그인이 필요합니다.</p><form method="post" action="/api/auth/login/start"><Button type="submit">조직 SSO 로그인</Button></form></div>; }
