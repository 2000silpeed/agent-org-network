"use client";

import { useState } from "react";
import {
  FileText,
  GitCommitHorizontal,
  Check,
  Ban,
  Loader2,
  AlertTriangle,
  CheckCircle2,
  Sparkles,
} from "lucide-react";
import { PageHeader } from "@/components/app-shell/page-header";
import { LoginGate } from "@/components/session/login-gate";
import { useSession } from "@/components/session/session-context";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { Tag } from "@/components/ui/tag";
import { StepProgress } from "@/components/ui/step-progress";
import {
  runAuthor,
  publishAuthor,
  AuthorError,
  type AuthorRunResult,
  type AuthorPublishResult,
  type Disposition,
} from "@/lib/author-api";
import { cn } from "@/lib/utils";

const DEMO_DOCUMENT = `2026년 인사·복리후생 규정 개정안

1. 연차 이월 — 미사용 연차는 최대 5일까지 익년 6월 말까지 이월된다. 이월 신청은 12월 회계 마감 전에 인사팀에 제출한다.

2. 경조 휴가 — 본인 결혼 5영업일, 자녀 결혼 1일, 배우자 출산 10일. 사유 발생일로부터 30일 이내 사용한다.

3. 표준 NDA 유효기간 — 비밀유지계약 기본 유효기간은 계약 종료 후 3년. (법무 소관)`;

export default function AuthorPage() {
  return (
    <div className="flex flex-col">
      <PageHeader
        surface="Author"
        persona="Owner"
        title="지식 작성"
        description="문서를 올리면 답변에 쓸 지식으로 정리합니다. 검토해 승인한 내용만 조직에 공유됩니다."
      />
      <LoginGate surface="지식 작성" requiredRole="owner">
        <AuthorWorkspace />
      </LoginGate>
    </div>
  );
}

function AuthorWorkspace() {
  const { identity } = useSession();
  const agentId = identity?.agentId ?? "";

  const [document, setDocument] = useState(DEMO_DOCUMENT);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<AuthorRunResult | null>(null);
  const [dispositions, setDispositions] = useState<Record<string, Disposition>>({});
  const [edits, setEdits] = useState<Record<string, string>>({}); // concept_id → edited core_question
  const [publishing, setPublishing] = useState(false);
  const [published, setPublished] = useState<AuthorPublishResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    if (!document.trim() || running) return;
    setRunning(true);
    setError(null);
    setPublished(null);
    try {
      const r = await runAuthor(agentId, document);
      setResult(r);
      // every extracted concept starts 승인; dropped(over-claim) are excluded server-side.
      const init: Record<string, Disposition> = {};
      for (const c of r.concepts) init[c.concept_id] = "approved";
      setDispositions(init);
      setEdits({});
    } catch (e) {
      setError(e instanceof AuthorError ? e.message : "분석에 실패했습니다.");
      setResult(null);
    } finally {
      setRunning(false);
    }
  }

  async function publish() {
    if (!result || publishing) return;
    setPublishing(true);
    setError(null);
    try {
      // run is stateless — carry the concept content back so the backend can
      // rebuild the OkfDocumentDraft (domain/title/body required; core_question edited).
      const concepts = result.concepts.map((c) => {
        const editedQ = edits[c.concept_id];
        const isEdited = editedQ !== undefined && editedQ !== c.core_question;
        const disposition: Disposition =
          dispositions[c.concept_id] === "rejected"
            ? "rejected"
            : isEdited
              ? "edited"
              : "approved";
        return {
          concept_id: c.concept_id,
          disposition,
          title: c.title,
          core_question: isEdited ? editedQ : c.core_question,
          body: c.body,
          domain: c.domain,
        };
      });
      const res = await publishAuthor(agentId, concepts);
      setPublished(res);
    } catch (e) {
      setError(e instanceof AuthorError ? e.message : "배포에 실패했습니다.");
    } finally {
      setPublishing(false);
    }
  }

  function setDisp(conceptId: string, d: Disposition) {
    setDispositions((prev) => ({ ...prev, [conceptId]: d }));
  }

  const concepts = result?.concepts ?? [];
  const counts = concepts.reduce(
    (acc, c) => {
      const d = dispositions[c.concept_id] ?? "approved";
      if (d === "rejected") acc.rejected += 1;
      else if (edits[c.concept_id] !== undefined && edits[c.concept_id] !== c.core_question)
        acc.edited += 1;
      else acc.approved += 1;
      return acc;
    },
    { approved: 0, edited: 0, rejected: 0 },
  );

  return (
    <div className="flex flex-col">
      <div className="flex flex-col gap-ds-16 px-ds-16 py-ds-16 md:px-ds-24">
        {/* source input — raw stays local (owner env) */}
        <section className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-16 py-ds-12">
          <div className="mb-ds-8 flex items-center gap-ds-8">
            <FileText aria-hidden className="h-[18px] w-[18px] text-[var(--ds-color-ink-subtle)]" />
            <h2 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
              원본 문서
            </h2>
            <span className="ml-auto text-xs text-[var(--ds-color-ink-subtle)]">
              담당 {agentId || "—"} · raw는 로컬에만
            </span>
          </div>
          <textarea
            value={document}
            onChange={(e) => setDocument(e.target.value)}
            rows={6}
            className="ds-scrollbar-thin w-full resize-y rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)] px-ds-12 py-ds-8 text-sm text-[var(--ds-color-ink)] placeholder:text-[var(--ds-color-ink-subtle)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ds-color-primary)]"
            placeholder="owner 환경의 문서·노트·위키를 붙여넣으세요 (raw는 중앙으로 가지 않습니다)"
          />
          <div className="mt-ds-8 flex justify-end">
            <Button variant="primary" size="md" onClick={() => void run()} loading={running} disabled={running || !document.trim()}>
              {!running && <Sparkles aria-hidden className="h-4 w-4" />}
              개념 추출
            </Button>
          </div>
        </section>

        {error && (
          <div role="alert" className="flex items-center gap-ds-8 rounded-md border border-[var(--ds-color-danger)] bg-[color-mix(in_srgb,var(--ds-color-danger)_8%,transparent)] px-ds-12 py-ds-8 text-sm text-[var(--ds-color-ink)]">
            <AlertTriangle aria-hidden className="h-4 w-4 shrink-0 text-[var(--ds-color-danger)]" />
            {error}
          </div>
        )}

        {result && (
          <>
            {/* staged stepper */}
            <div className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-16 py-ds-12">
              <StepProgress steps={result.stages.map((s) => ({ id: s.key, label: s.label, state: s.state }))} />
            </div>

            {/* concept drafts (interactive) */}
            <div>
              <h2 className="mb-ds-12 font-heading text-md font-semibold text-[var(--ds-color-ink)]">
                개념 초안 ({concepts.length})
              </h2>
              {concepts.length === 0 ? (
                <p className="text-sm text-[var(--ds-color-ink-subtle)]">추출된 개념이 없습니다.</p>
              ) : (
                <div className="grid grid-cols-1 gap-ds-16 lg:grid-cols-2 xl:grid-cols-3">
                  {concepts.map((c) => {
                    const disp = dispositions[c.concept_id] ?? "approved";
                    const rejected = disp === "rejected";
                    return (
                      <div
                        key={c.concept_id}
                        className={cn(
                          "flex flex-col overflow-hidden rounded-lg border bg-[var(--ds-color-surface)] transition-opacity",
                          rejected
                            ? "border-[var(--ds-color-border)] opacity-55"
                            : "border-[var(--ds-color-border)]",
                        )}
                      >
                        <div className="flex flex-col gap-ds-8 border-b border-[var(--ds-color-border)] px-ds-16 py-ds-12">
                          <div className="flex flex-wrap items-center gap-ds-8">
                            <span className="font-mono text-xs text-[var(--ds-color-ink-subtle)]">
                              {c.concept_id}
                            </span>
                            <Tag tone="neutral">{c.domain}</Tag>
                          </div>
                          <p className="text-sm font-medium text-[var(--ds-color-ink)]">{c.title}</p>
                        </div>
                        <div className="flex flex-1 flex-col gap-ds-8 px-ds-16 py-ds-12">
                          <label className="text-xs font-medium text-[var(--ds-color-ink-subtle)]">
                            core_question
                          </label>
                          <input
                            value={edits[c.concept_id] ?? c.core_question}
                            onChange={(e) =>
                              setEdits((prev) => ({ ...prev, [c.concept_id]: e.target.value }))
                            }
                            disabled={rejected}
                            className="rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)] px-ds-8 py-[6px] text-sm text-[var(--ds-color-ink)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ds-color-primary)] disabled:opacity-60"
                          />
                          <p className="line-clamp-3 text-xs text-[var(--ds-color-ink-muted)]">{c.body}</p>
                        </div>
                        <div className="flex items-center gap-ds-8 border-t border-[var(--ds-color-border)] px-ds-16 py-ds-8">
                          <Button
                            size="sm"
                            variant={!rejected ? "primary" : "secondary"}
                            onClick={() => setDisp(c.concept_id, "approved")}
                          >
                            <Check aria-hidden className="h-4 w-4" />
                            승인
                          </Button>
                          <Button
                            size="sm"
                            variant={rejected ? "danger" : "secondary"}
                            onClick={() => setDisp(c.concept_id, "rejected")}
                          >
                            <Ban aria-hidden className="h-4 w-4" />
                            거부
                          </Button>
                          {edits[c.concept_id] !== undefined &&
                            edits[c.concept_id] !== c.core_question &&
                            !rejected && (
                              <StatusBadge tone="warning" label="수정됨" />
                            )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* dropped — over-claim (admit_okf), excluded from publish */}
            {result.dropped.length > 0 && (
              <div className="rounded-lg border border-dashed border-[var(--ds-color-border)] bg-[var(--ds-color-surface-muted)] px-ds-16 py-ds-12">
                <div className="mb-ds-8 flex items-center gap-ds-8">
                  <Ban aria-hidden className="h-4 w-4 text-[var(--ds-color-danger)]" />
                  <h3 className="text-sm font-medium text-[var(--ds-color-ink)]">
                    권한 밖 개념 — 배포 제외 ({result.dropped.length})
                  </h3>
                </div>
                <ul className="flex flex-col gap-ds-4">
                  {result.dropped.map((d) => (
                    <li key={d.concept_id} className="flex flex-wrap items-center gap-ds-8 text-xs text-[var(--ds-color-ink-muted)]">
                      <span className="font-mono text-[var(--ds-color-ink-subtle)]">{d.concept_id}</span>
                      <span>— {d.reason}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {published && (
              <div role="status" className="flex items-start gap-ds-8 rounded-md border border-[color-mix(in_srgb,var(--ds-color-success)_45%,transparent)] bg-[color-mix(in_srgb,var(--ds-color-success)_8%,transparent)] px-ds-16 py-ds-12 text-sm text-[var(--ds-color-ink)]">
                <CheckCircle2 aria-hidden className="mt-[2px] h-4 w-4 shrink-0 text-[var(--ds-color-success)]" />
                <div>
                  <p className="font-medium">
                    {published.published
                      ? `배포 완료 — 목차 ${published.published.concept_count}개념이 중앙에 게시되었습니다.`
                      : "커밋 완료 — 승인된 개념이 없어 중앙 배포는 생략되었습니다."}
                  </p>
                  <p className="mt-ds-2 text-xs text-[var(--ds-color-ink-muted)]">
                    커밋 {published.committed.sha ? published.committed.sha.slice(0, 12) : "(owner git)"} ·
                    파일 {published.committed.files.length}개 · 중앙은 목차만(raw·본문 비공유).
                  </p>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* disposition summary + commit/deploy bar */}
      {result && concepts.length > 0 && (
        <div className="sticky bottom-0 z-10 mt-auto border-t border-[var(--ds-color-border)] bg-[var(--ds-color-surface)]/95 px-ds-16 py-ds-12 backdrop-blur md:px-ds-24">
          <div className="flex flex-col gap-ds-12 md:flex-row md:items-center md:justify-between">
            <p className="text-sm text-[var(--ds-color-ink-muted)]">
              <span className="font-medium text-[var(--ds-color-ink)]">
                승인 {counts.approved} · 수정 {counts.edited} · 거부 {counts.rejected}
              </span>{" "}
              — 거부분은 배포되지 않습니다.
            </p>
            <div className="flex flex-wrap items-center gap-ds-8">
              <Button variant="primary" size="md" onClick={() => void publish()} loading={publishing} disabled={publishing}>
                {!publishing && <GitCommitHorizontal aria-hidden className="h-4 w-4" />}
                커밋 · 배포
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
