"use client";

import { useCallback, useEffect, useState } from "react";
import {
  GitMerge,
  UserCheck,
  RefreshCw,
  FileSearch,
  Check,
  Pencil,
  X,
  ArrowRightLeft,
  Loader2,
  AlertTriangle,
  Quote,
} from "lucide-react";
import Image from "next/image";
import {
  getInboxCases,
  getBackupReviews,
  getReeval,
  postConcur,
  postReevalReview,
  fetchCaseDocument,
  InboxError,
  type ConflictCase,
  type BackupReviewItem,
  type ReevalItem,
  type ReevalOutcomeKind,
  type ConsensusOutcome,
  type FetchDocumentResult,
} from "@/lib/inbox-api";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/card";
import { Tag } from "@/components/ui/tag";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { useSession } from "@/components/session/session-context";
import { cn } from "@/lib/utils";

type TabId = "contested" | "backup" | "reeval";

export function InboxTabs() {
  const { userId } = useSession();
  const [active, setActive] = useState<TabId>("contested");

  // Real data — refetched whenever the session identity changes.
  const [cases, setCases] = useState<ConflictCase[]>([]);
  const [reviews, setReviews] = useState<BackupReviewItem[]>([]);
  const [reevals, setReevals] = useState<ReevalItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [c, r, rv] = await Promise.all([
        getInboxCases(),
        getBackupReviews().catch(() => [] as BackupReviewItem[]),
        getReeval().catch(() => [] as ReevalItem[]),
      ]);
      setCases(c);
      setReviews(r);
      setReevals(rv);
    } catch (e) {
      setError(e instanceof Error ? e.message : "처리함을 불러오지 못했습니다.");
      setCases([]);
      setReviews([]);
      setReevals([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, userId]);

  const tabs: { id: TabId; label: string; icon: typeof GitMerge; count: number }[] = [
    { id: "contested", label: "다툼", icon: GitMerge, count: cases.length },
    { id: "backup", label: "백업 답", icon: UserCheck, count: reviews.length },
    { id: "reeval", label: "재평가", icon: RefreshCw, count: reevals.length },
  ];

  return (
    <div className="px-ds-16 py-ds-16 md:px-ds-24">
      <div
        role="tablist"
        aria-label="처리함 탭"
        className="ds-scrollbar-thin mb-ds-16 flex items-center gap-ds-8 overflow-x-auto border-b border-[var(--ds-color-border)]"
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
                  : "border-transparent text-[var(--ds-color-ink-subtle)] hover:text-[var(--ds-color-ink-muted)]",
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
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="-mb-px ml-auto inline-flex shrink-0 items-center gap-ds-4 px-ds-8 py-ds-8 text-xs font-medium text-[var(--ds-color-ink-subtle)] transition-colors hover:text-[var(--ds-color-ink)] disabled:opacity-50"
          aria-label="새로고침"
        >
          <RefreshCw aria-hidden className={cn("h-[14px] w-[14px]", loading && "animate-ds-spin")} />
          새로고침
        </button>
      </div>

      {error && (
        <div
          role="alert"
          className="mb-ds-16 flex items-center gap-ds-8 rounded-md border border-[var(--ds-color-danger)] bg-[color-mix(in_srgb,var(--ds-color-danger)_8%,transparent)] px-ds-12 py-ds-8 text-sm text-[var(--ds-color-ink)]"
        >
          <AlertTriangle aria-hidden className="h-4 w-4 shrink-0 text-[var(--ds-color-danger)]" />
          {error}
        </div>
      )}

      {active === "contested" && (
        <ContestedPanel cases={cases} loading={loading} onConcurDone={() => void refresh()} />
      )}
      {active === "backup" && <BackupPanel reviews={reviews} loading={loading} />}
      {active === "reeval" && (
        <ReevalPanel reevals={reevals} loading={loading} onReviewDone={() => void refresh()} />
      )}
    </div>
  );
}

/* ----------------------------- 다툼 (real) ----------------------------- */

function ContestedPanel({
  cases,
  loading,
  onConcurDone,
}: {
  cases: ConflictCase[];
  loading: boolean;
  onConcurDone: () => void;
}) {
  if (loading && cases.length === 0) return <PanelSkeleton label="다툼 케이스" />;
  if (cases.length === 0) {
    return <EmptyState label="담당이 갈리는 다툼 케이스가 없습니다." />;
  }
  return (
    <div role="tabpanel" className="flex flex-col gap-ds-16" aria-label="다툼 케이스">
      {cases.map((c) => (
        <ContestedCard key={c.case_id} c={c} onConcurDone={onConcurDone} />
      ))}
    </div>
  );
}

function ContestedCard({ c, onConcurDone }: { c: ConflictCase; onConcurDone: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<ConsensusOutcome | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function concur(agentId: string) {
    setBusy(agentId);
    setActionError(null);
    try {
      const result = await postConcur(c.case_id, agentId);
      setOutcome(result);
      if (result.type === "agreed") {
        // Case resolved — let the parent refetch so it drops from the list.
        setTimeout(onConcurDone, 900);
      }
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "담당 지정에 실패했습니다.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card elevated>
      <CardHeader>
        <div className="flex items-center gap-ds-8">
          <StatusBadge tone="warning" label="담당 미정" />
          {c.intent && <Tag tone="neutral">{c.intent}</Tag>}
        </div>
        <p className="text-md font-medium text-[var(--ds-color-ink)]">{c.question}</p>
      </CardHeader>
      <CardBody className="flex flex-col gap-ds-12">
        <div className="grid grid-cols-1 gap-ds-12 md:grid-cols-2">
          {c.candidates.map((cand) => (
            <CandidateCard key={cand.agent_id} caseId={c.case_id} cand={cand} />
          ))}
        </div>
      </CardBody>
      <CardFooter className="flex-wrap">
        {outcome ? (
          <OutcomeBanner outcome={outcome} />
        ) : (
          <>
            <span className="mr-auto text-xs text-[var(--ds-color-ink-subtle)]">
              커버리지를 보고 담당을 지정하세요.
            </span>
            {c.candidates.map((cand) => (
              <Button
                key={cand.agent_id}
                size="sm"
                variant="secondary"
                disabled={busy !== null}
                loading={busy === cand.agent_id}
                onClick={() => void concur(cand.agent_id)}
              >
                {busy !== cand.agent_id && <ArrowRightLeft aria-hidden className="h-4 w-4" />}
                {cand.agent_id} 지정
              </Button>
            ))}
          </>
        )}
      </CardFooter>
      {actionError && (
        <div className="border-t border-[var(--ds-color-border)] px-ds-16 py-ds-8 text-xs text-[var(--ds-color-danger)]">
          {actionError}
        </div>
      )}
    </Card>
  );
}

function OutcomeBanner({ outcome }: { outcome: ConsensusOutcome }) {
  if (outcome.type === "agreed") {
    return (
      <StatusBadge
        tone="success"
        label={`합의 완료 — ${outcome.primary} 담당 (${outcome.intent})`}
      />
    );
  }
  if (outcome.type === "still_open") {
    return (
      <StatusBadge
        tone="info"
        label={`내 표 기록됨 — 나머지 대기: ${outcome.pending_owners.join(", ") || "없음"}`}
      />
    );
  }
  return <StatusBadge tone="danger" label="교착 — 매니저 에스컬레이션" />;
}

function CandidateCard({
  caseId,
  cand,
}: {
  caseId: string;
  cand: ConflictCase["candidates"][number];
}) {
  const concepts = cand.relevant_concepts ?? [];
  return (
    <div className="rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-12">
      <div className="flex flex-wrap items-center justify-between gap-ds-8">
        <span className="font-medium text-[var(--ds-color-ink)]">
          {cand.agent_id}
          <span className="ml-ds-4 text-xs font-normal text-[var(--ds-color-ink-subtle)]">
            {cand.owner}
          </span>
        </span>
        <StatusBadge
          tone={concepts.length > 0 ? "success" : "neutral"}
          label={
            concepts.length > 0 ? `연관 개념 ${concepts.length}건` : "연관 개념 없음"
          }
        />
      </div>

      {cand.summary && (
        <p className="mt-ds-8 text-xs text-[var(--ds-color-ink-muted)]">{cand.summary}</p>
      )}

      <div className="mt-ds-8 flex flex-wrap items-center gap-ds-4">
        {(cand.domains ?? []).map((d) => (
          <Tag key={d} tone="neutral">
            {d}
          </Tag>
        ))}
      </div>

      {concepts.length > 0 && (
        <div className="mt-ds-8 flex flex-wrap items-center gap-ds-4">
          <span className="text-xs text-[var(--ds-color-ink-subtle)]">질문-연관 개념:</span>
          {concepts.map((rc) => (
            <DocumentChip key={rc.id} caseId={caseId} agentId={cand.agent_id} concept={rc} />
          ))}
        </div>
      )}
    </div>
  );
}

function DocumentChip({
  caseId,
  agentId,
  concept,
}: {
  caseId: string;
  agentId: string;
  concept: { id: string; label: string; core_question: string };
}) {
  const [busy, setBusy] = useState(false);
  const [doc, setDoc] = useState<FetchDocumentResult | null>(null);
  const [open, setOpen] = useState(false);

  async function fetchDoc() {
    if (doc) {
      setOpen((v) => !v);
      return;
    }
    setBusy(true);
    try {
      const result = await fetchCaseDocument(caseId, agentId, concept.id);
      setDoc(result);
      setOpen(true);
    } catch (e) {
      setDoc({
        found: false,
        available: false,
        message: e instanceof InboxError ? e.message : "문서를 가져오지 못했습니다.",
      });
      setOpen(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex flex-col">
      <button
        type="button"
        onClick={() => void fetchDoc()}
        disabled={busy}
        className="inline-flex items-center gap-ds-4 rounded-sm bg-[color-mix(in_srgb,var(--ds-color-info)_14%,transparent)] px-ds-8 py-[3px] text-xs font-medium text-[var(--ds-color-info)] transition-opacity hover:opacity-80 disabled:opacity-50"
        title={concept.core_question}
      >
        {busy ? (
          <Loader2 aria-hidden className="h-[13px] w-[13px] animate-ds-spin" />
        ) : (
          <FileSearch aria-hidden className="h-[13px] w-[13px]" />
        )}
        {concept.label}
      </button>
      {open && doc && (
        <span className="mt-ds-4 block rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface-muted)] px-ds-8 py-ds-4 text-xs text-[var(--ds-color-ink-muted)]">
          {doc.found ? (
            <span className="flex items-start gap-ds-4">
              <Quote aria-hidden className="mt-[2px] h-3 w-3 shrink-0 text-[var(--ds-color-ink-subtle)]" />
              <span className="whitespace-pre-wrap">{doc.content}</span>
            </span>
          ) : (
            doc.message
          )}
        </span>
      )}
    </span>
  );
}

/* ---------------------------- 백업 답 (real) --------------------------- */

function BackupPanel({
  reviews,
  loading,
}: {
  reviews: BackupReviewItem[];
  loading: boolean;
}) {
  if (loading && reviews.length === 0) return <PanelSkeleton label="백업 답변" />;
  if (reviews.length === 0) {
    return (
      <EmptyState label="검토 대기 중인 백업 답변이 없습니다. (이 데모 구성에서는 백업 검토 store가 연결되어 있지 않습니다.)" />
    );
  }
  return (
    <div role="tabpanel" className="flex flex-col gap-ds-12" aria-label="백업 답변">
      {reviews.map((b) => (
        <Card key={b.item_id} elevated>
          <CardHeader>
            <div className="flex flex-wrap items-center gap-ds-8">
              <StatusBadge tone="info" label="백업 답변" />
              <span className="text-xs text-[var(--ds-color-ink-subtle)]">
                {b.agent_id} · {b.owner_id} 부재 · {b.answered_at}
              </span>
            </div>
            <p className="text-md font-medium text-[var(--ds-color-ink)]">{b.question}</p>
          </CardHeader>
          <CardBody>
            <p className="text-sm leading-normal text-[var(--ds-color-ink-muted)]">
              {b.backup_answer_text}
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

/* --------------------------- 재평가 (real) ----------------------------- */

function fmtReevalTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString("ko-KR", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function ReevalPanel({
  reevals,
  loading,
  onReviewDone,
}: {
  reevals: ReevalItem[];
  loading: boolean;
  onReviewDone: () => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actError, setActError] = useState<string | null>(null);

  async function act(itemId: string, kind: ReevalOutcomeKind) {
    setBusyId(itemId);
    setActError(null);
    try {
      await postReevalReview(itemId, kind);
      onReviewDone();
    } catch (e) {
      setActError(e instanceof InboxError ? e.message : "재평가 처분에 실패했습니다.");
    } finally {
      setBusyId(null);
    }
  }

  if (loading && reevals.length === 0) return <PanelSkeleton label="재평가 항목" />;
  if (reevals.length === 0)
    return <EmptyState label="지식 변경으로 stale된 과거 판례·답이 없습니다." />;

  return (
    <div role="tabpanel" className="flex flex-col gap-ds-12" aria-label="재평가 케이스">
      {actError && (
        <div
          role="alert"
          className="flex items-center gap-ds-8 rounded-md border border-[var(--ds-color-danger)] bg-[color-mix(in_srgb,var(--ds-color-danger)_8%,transparent)] px-ds-12 py-ds-8 text-sm text-[var(--ds-color-ink)]"
        >
          <AlertTriangle aria-hidden className="h-4 w-4 shrink-0 text-[var(--ds-color-danger)]" />
          {actError}
        </div>
      )}
      {reevals.map((r) => {
        const busy = busyId === r.item_id;
        return (
          <Card key={r.item_id} elevated>
            <CardHeader>
              <div className="flex flex-wrap items-center gap-ds-8">
                <StatusBadge tone="warning" label="stale · 재평가 필요" />
                <Tag tone="neutral">{r.subject_kind === "precedent" ? "판례" : "과거 답"}</Tag>
                <span className="text-xs text-[var(--ds-color-ink-subtle)]">
                  {fmtReevalTime(r.flagged_at)}
                </span>
              </div>
              <p className="text-md font-medium text-[var(--ds-color-ink)]">{r.question}</p>
            </CardHeader>
            <CardBody className="flex flex-col gap-ds-8">
              <div className="flex flex-wrap items-center gap-ds-4 text-xs text-[var(--ds-color-ink-muted)]">
                <span>변경 번들:</span>
                <Tag tone="neutral">{r.agent_id}</Tag>
                <span className="font-mono text-[var(--ds-color-ink-subtle)]">{r.trigger_sha}</span>
              </div>
              <p className="text-sm text-[var(--ds-color-ink-muted)]">{r.reason}</p>
            </CardBody>
            <CardFooter>
              <Button
                size="sm"
                variant="secondary"
                disabled={busy}
                onClick={() => void act(r.item_id, "keep")}
              >
                <Check aria-hidden className="h-4 w-4" />
                유지
              </Button>
              <Button
                size="sm"
                variant="primary"
                disabled={busy}
                onClick={() => void act(r.item_id, "reanswer")}
              >
                <RefreshCw aria-hidden className="h-4 w-4" />
                재답변
              </Button>
              <Button
                size="sm"
                variant="danger"
                disabled={busy}
                onClick={() => void act(r.item_id, "invalidate")}
              >
                <X aria-hidden className="h-4 w-4" />
                무효화
              </Button>
            </CardFooter>
          </Card>
        );
      })}
    </div>
  );
}

/* ------------------------------- shared -------------------------------- */

function PanelSkeleton({ label }: { label: string }) {
  return (
    <div
      role="status"
      aria-label={`${label} 불러오는 중`}
      className="flex items-center gap-ds-8 rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-16 py-ds-16 text-sm text-[var(--ds-color-ink-subtle)]"
    >
      <Loader2 aria-hidden className="h-4 w-4 animate-ds-spin" />
      {label} 불러오는 중…
    </div>
  );
}

function EmptyState({ label }: { label: string }) {
  return (
    <div className="flex flex-col items-center gap-ds-8 rounded-lg border border-dashed border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-16 py-ds-24 text-center">
      <Image
        src="/brand/empty-inbox.png"
        alt=""
        width={320}
        height={181}
        className="mb-ds-4 w-full max-w-[260px] opacity-90"
      />
      <p className="text-sm text-[var(--ds-color-ink-muted)]">{label}</p>
    </div>
  );
}
