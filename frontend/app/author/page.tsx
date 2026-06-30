"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  FileText,
  GitCommitHorizontal,
  Check,
  Ban,
  AlertTriangle,
  CheckCircle2,
  Sparkles,
  Upload,
  Library,
  ShieldAlert,
  FileCode,
  Pencil,
  Trash2,
  X,
  GitMerge,
} from "lucide-react";
import { PageHeader } from "@/components/app-shell/page-header";
import { LoginGate } from "@/components/session/login-gate";
import { useSession } from "@/components/session/session-context";
import { IdentitySwitcher } from "@/components/session/identity-switcher";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { Tag } from "@/components/ui/tag";
import { StepProgress } from "@/components/ui/step-progress";
import {
  runAuthor,
  publishAuthor,
  fetchAuthorIndex,
  fetchConcept,
  updateConcept,
  deleteConcept,
  checkDedup,
  AuthorError,
  type AuthorRunResult,
  type AuthorPublishResult,
  type AuthorIndexResult,
  type AuthorConceptDetail,
  type AuthorConcept,
  type Disposition,
  type DedupCandidate,
} from "@/lib/author-api";
import { cn } from "@/lib/utils";

// owner 도메인에 맞는 샘플 문서 — 선택한 신원의 담당 카드(agentId)별로 다르게 채운다.
// 그래야 추출된 개념이 그 owner의 owned-domain과 맞아 게시까지 한 번 관통한다(데모용).
// 실제로는 owner가 자기 문서를 붙여넣거나 파일로 올린다(raw는 로컬에만).
const SAMPLE_DOCS: Record<string, string> = {
  contract_ops: `계약 검토 표준 절차

1. 모든 외부 계약서는 법무팀 검토를 거친다. 검토 요청은 계약 체결 7영업일 전까지 제출한다.
2. 표준 NDA 유효기간은 계약 종료 후 3년이다.
3. 위약금 조항은 계약 금액의 10%를 상한으로 한다.`,
  cs_ops: `고객 환불·보상 정책

1. 단순 변심 환불은 수령 후 7일 이내 가능하며 왕복 배송비는 고객 부담이다.
2. 제품 하자 환불은 30일 이내 전액 환불하고 배송비는 회사가 부담한다.
3. 배송 지연 보상은 3영업일 초과 시 주문 금액의 5%를 적립금으로 지급한다.`,
  finance_ops: `가격·보상 운영 기준

1. 표준 단가는 분기마다 재산정하며, 10% 이상 인상은 사전 공지 후 적용한다.
2. 대량 구매 할인은 1만 개 이상 주문에 5% 적용한다.
3. 정산 오류 보상은 확인일로부터 5영업일 이내 차액을 환급한다.`,
  hr_ops: `2026년 인사·복리후생 규정

1. 연차 이월 — 미사용 연차는 최대 5일까지 익년 6월 말까지 이월된다.
2. 경조 휴가 — 본인 결혼 5영업일, 자녀 결혼 1일, 배우자 출산 10일.
3. 채용 수습기간은 3개월이며, 평가 후 정규 전환한다.`,
  it_ops: `계정·접근권한·보안 정책

1. 신규 계정은 입사 첫날 발급하고, 퇴사 시 당일 즉시 비활성화한다.
2. 접근권한은 직무 기반(RBAC)으로 부여하며 분기마다 재검토한다.
3. 보안 사고 의심 시 1차 차단 후 30분 이내 보안팀에 신고한다.`,
};

const FALLBACK_SAMPLE = SAMPLE_DOCS.contract_ops;

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

  // operator/root_manager 등 카드가 없는 신원은 저작 대상이 없다 — 헛된 요청(400) 대신
  // owner 신원으로 전환하라는 안내를 보여준다(에러 방지).
  if (!agentId) {
    return <NoCardNotice />;
  }
  // key=agentId — 신원 전환 시 워크스페이스를 리마운트해 샘플·추출 상태를 새 owner로 초기화.
  return <OwnerWorkspace key={agentId} agentId={agentId} />;
}

function NoCardNotice() {
  return (
    <div className="px-ds-16 py-ds-24 md:px-ds-24">
      <div className="mx-auto flex max-w-xl flex-col gap-ds-12 rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-24">
        <div className="flex items-center gap-ds-12">
          <span className="flex h-10 w-10 items-center justify-center rounded-md bg-[var(--ds-color-surface-tint)]">
            <ShieldAlert aria-hidden className="h-5 w-5 text-[var(--ds-color-warning)]" />
          </span>
          <div>
            <h2 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
              담당 카드가 있는 신원이 필요합니다
            </h2>
            <p className="text-sm text-[var(--ds-color-ink-muted)]">
              지식 작성은 자기 담당(Owner) 지식만 다룹니다. 운영자 신원에는 작성할 카드가 없습니다.
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-ds-8">
          <span className="text-sm text-[var(--ds-color-ink-subtle)]">Owner 신원으로 전환:</span>
          <IdentitySwitcher />
        </div>
      </div>
    </div>
  );
}

function OwnerWorkspace({ agentId }: { agentId: string }) {
  const [document, setDocument] = useState(SAMPLE_DOCS[agentId] ?? FALLBACK_SAMPLE);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<AuthorRunResult | null>(null);
  const [dispositions, setDispositions] = useState<Record<string, Disposition>>({});
  const [edits, setEdits] = useState<Record<string, string>>({}); // concept_id → edited core_question
  const [publishing, setPublishing] = useState(false);
  const [published, setPublished] = useState<AuthorPublishResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [library, setLibrary] = useState<AuthorIndexResult | null>(null);
  const [dedupCandidates, setDedupCandidates] = useState<DedupCandidate[]>([]);
  const [mergingId, setMergingId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const reloadLibrary = useCallback(() => {
    fetchAuthorIndex(agentId)
      .then(setLibrary)
      .catch(() => setLibrary(null));
  }, [agentId]);

  // 이미 게시된 목차를 불러온다(마운트 + 신원 변경 시).
  useEffect(() => {
    reloadLibrary();
  }, [reloadLibrary]);

  // .txt/.md 파일을 읽어 원본 문서에 이어붙인다(raw는 로컬에만 머문다).
  async function onFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    const parts: string[] = [];
    for (const f of Array.from(files)) {
      try {
        const text = await f.text();
        if (text.trim()) parts.push(`# ${f.name}\n${text.trim()}`);
      } catch {
        // 개별 파일 읽기 실패는 건너뛴다.
      }
    }
    if (parts.length === 0) return;
    setDocument((prev) => {
      const joined = parts.join("\n\n");
      return prev.trim() ? `${prev.trim()}\n\n${joined}` : joined;
    });
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

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
      setDedupCandidates([]);
      // 의미 중복 탐지(ADR 0032 결정 C) — 게시 라이브러리와 비슷한 신규 개념이 있는지
      // 읽기 전용으로 확인한다. AON_EMBEDDER 비활성(기본)이면 빈 후보가 와 조용히 무력.
      if (r.concepts.length > 0) {
        checkDedup(agentId, r.concepts)
          .then((res) => setDedupCandidates(res.candidates))
          .catch(() => setDedupCandidates([]));
      }
    } catch (e) {
      setError(e instanceof AuthorError ? e.message : "분석에 실패했습니다.");
      setResult(null);
    } finally {
      setRunning(false);
    }
  }

  // owner가 "기존 개념과 같다"고 확정 — 기존 개념을 신규 추출 내용으로 갱신(PUT 재사용)하고
  // 신규는 거부 처리해 배포 대상에서 뺀다(ADR 0032 결정 C4 — 새 병합 연산 0).
  async function mergeIntoExisting(concept: AuthorConcept, existingConceptId: string) {
    setMergingId(concept.concept_id);
    setError(null);
    try {
      await updateConcept(agentId, existingConceptId, {
        title: concept.title,
        core_question: edits[concept.concept_id] ?? concept.core_question,
        body: concept.body,
        domain: concept.domain,
      });
      setDisp(concept.concept_id, "rejected");
      setDedupCandidates((prev) => prev.filter((d) => d.new_concept_id !== concept.concept_id));
      reloadLibrary();
    } catch (e) {
      setError(e instanceof AuthorError ? e.message : "병합에 실패했습니다.");
    } finally {
      setMergingId(null);
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
      reloadLibrary(); // 게시 후 라이브러리(목차) 갱신
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
          <div className="mt-ds-8 flex flex-wrap items-center justify-between gap-ds-8">
            <div className="flex items-center gap-ds-8">
              <input
                ref={fileInputRef}
                type="file"
                accept=".txt,.md,.markdown,text/plain,text/markdown"
                multiple
                onChange={(e) => void onFiles(e.target.files)}
                className="hidden"
              />
              <Button
                variant="secondary"
                size="md"
                onClick={() => fileInputRef.current?.click()}
                disabled={running}
              >
                <Upload aria-hidden className="h-4 w-4" />
                파일 올리기
              </Button>
              <span className="text-xs text-[var(--ds-color-ink-subtle)]">.txt · .md</span>
            </div>
            <Button variant="primary" size="md" onClick={() => void run()} loading={running} disabled={running || !document.trim()}>
              {!running && <Sparkles aria-hidden className="h-4 w-4" />}
              개념 추출
            </Button>
          </div>
        </section>

        {/* 이미 게시된 목차(라이브러리) — owner가 만든 개념을 보고 편집·삭제한다 */}
        <LibraryPanel library={library} agentId={agentId} onChanged={reloadLibrary} />

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
                    const dupMatches = dedupCandidates.filter((d) => d.new_concept_id === c.concept_id);
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
                          {!rejected &&
                            dupMatches.map((d) => {
                              const existingTitle =
                                library?.concepts.find((lc) => lc.id === d.existing_concept_id)?.label ??
                                d.existing_concept_id;
                              const pct = Math.round(d.similarity * 100);
                              return (
                                <div
                                  key={d.existing_concept_id}
                                  className="flex flex-col gap-ds-4 rounded-md border border-[var(--ds-color-warning)] bg-[color-mix(in_srgb,var(--ds-color-warning)_8%,transparent)] px-ds-8 py-ds-8 text-xs"
                                >
                                  <div className="flex items-center gap-ds-4 text-[var(--ds-color-ink)]">
                                    <GitMerge aria-hidden className="h-[14px] w-[14px] shrink-0 text-[var(--ds-color-warning)]" />
                                    <span>
                                      {d.grade === "auto_suggest" ? "거의 동일한" : "비슷한"} 기존 개념 발견
                                      — <span className="font-medium">{existingTitle}</span> ({pct}%)
                                    </span>
                                  </div>
                                  <div>
                                    <Button
                                      size="sm"
                                      variant="secondary"
                                      onClick={() => void mergeIntoExisting(c, d.existing_concept_id)}
                                      loading={mergingId === c.concept_id}
                                      disabled={mergingId === c.concept_id}
                                    >
                                      <GitMerge aria-hidden className="h-[14px] w-[14px]" />
                                      기존 개념으로 병합
                                    </Button>
                                  </div>
                                </div>
                              );
                            })}
                          {c.okf_markdown && (
                            <details className="group mt-ds-4 rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)]">
                              <summary className="flex cursor-pointer list-none items-center gap-ds-4 px-ds-8 py-[6px] text-xs font-medium text-[var(--ds-color-ink-subtle)] hover:text-[var(--ds-color-ink-muted)]">
                                <FileCode aria-hidden className="h-[14px] w-[14px]" />
                                OKF 원문 (프론트매터 + 본문)
                              </summary>
                              <pre className="ds-scrollbar-thin overflow-x-auto border-t border-[var(--ds-color-border)] px-ds-8 py-ds-8 font-mono text-[11px] leading-relaxed text-[var(--ds-color-ink-muted)]">
                                {c.okf_markdown}
                              </pre>
                            </details>
                          )}
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

// 이미 중앙에 게시된 목차를 보여준다(owner 라이브러리). 본문은 없다 — 목차 한 줄씩(중앙 비소유).
function LibraryPanel({
  library,
  agentId,
  onChanged,
}: {
  library: AuthorIndexResult | null;
  agentId: string;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState<string | null>(null); // concept_id being edited
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 번들 메타(index.md·type=index)는 지식 개념이 아니므로 관리 목록에서 숨긴다.
  const concepts = (library?.concepts ?? []).filter((c) => c.type !== "index");
  if (concepts.length === 0) return null;

  async function onDelete(conceptId: string) {
    setBusyId(conceptId);
    setError(null);
    try {
      await deleteConcept(agentId, conceptId);
      setPendingDelete(null);
      onChanged();
    } catch (e) {
      setError(e instanceof AuthorError ? e.message : "삭제에 실패했습니다.");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-16 py-ds-12">
      <div className="mb-ds-8 flex flex-wrap items-center gap-ds-8">
        <Library aria-hidden className="h-[18px] w-[18px] text-[var(--ds-color-ink-subtle)]" />
        <h2 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
          게시된 개념
        </h2>
        <StatusBadge tone="success" label={`${concepts.length}개`} />
        <span className="ml-auto text-xs text-[var(--ds-color-ink-subtle)]">
          중앙에 공유된 목차 · 본문은 로컬에만
        </span>
      </div>

      {error && (
        <div role="alert" className="mb-ds-8 flex items-center gap-ds-8 rounded-md border border-[var(--ds-color-danger)] bg-[color-mix(in_srgb,var(--ds-color-danger)_8%,transparent)] px-ds-12 py-ds-8 text-sm text-[var(--ds-color-ink)]">
          <AlertTriangle aria-hidden className="h-4 w-4 shrink-0 text-[var(--ds-color-danger)]" />
          {error}
        </div>
      )}

      <ul className="flex flex-col divide-y divide-[var(--ds-color-border)]">
        {concepts.map((c) =>
          editing === c.id ? (
            <li key={c.id} className="py-ds-8">
              <ConceptEditor
                agentId={agentId}
                conceptId={c.id}
                onClose={() => setEditing(null)}
                onSaved={() => {
                  setEditing(null);
                  onChanged();
                }}
              />
            </li>
          ) : (
            <li key={c.id} className="flex flex-wrap items-center gap-ds-8 py-ds-8">
              <span className="font-mono text-xs text-[var(--ds-color-ink-subtle)]">{c.id}</span>
              <Tag tone="neutral">{c.domain}</Tag>
              <span className="text-sm font-medium text-[var(--ds-color-ink)]">{c.label}</span>
              <span className="min-w-0 flex-1 truncate text-xs text-[var(--ds-color-ink-muted)]">
                — {c.core_question}
              </span>
              {pendingDelete === c.id ? (
                <span className="flex items-center gap-ds-8">
                  <span className="text-xs text-[var(--ds-color-danger)]">삭제할까요?</span>
                  <Button size="sm" variant="danger" onClick={() => void onDelete(c.id)} loading={busyId === c.id} disabled={busyId === c.id}>
                    삭제
                  </Button>
                  <Button size="sm" variant="secondary" onClick={() => setPendingDelete(null)} disabled={busyId === c.id}>
                    취소
                  </Button>
                </span>
              ) : (
                <span className="flex items-center gap-ds-4">
                  <Button size="sm" variant="ghost" onClick={() => { setError(null); setEditing(c.id); }}>
                    <Pencil aria-hidden className="h-[14px] w-[14px]" />
                    편집
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => { setError(null); setPendingDelete(c.id); }}>
                    <Trash2 aria-hidden className="h-[14px] w-[14px]" />
                    삭제
                  </Button>
                </span>
              )}
            </li>
          ),
        )}
      </ul>
    </section>
  );
}

// 게시 개념 인라인 편집기 — 마운트 시 본문 포함 상세를 GET, 저장 시 PUT(인덱스 재도출).
function ConceptEditor({
  agentId,
  conceptId,
  onClose,
  onSaved,
}: {
  agentId: string;
  conceptId: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [detail, setDetail] = useState<AuthorConceptDetail | null>(null);
  const [coreQuestion, setCoreQuestion] = useState("");
  const [body, setBody] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchConcept(agentId, conceptId)
      .then((d) => {
        if (!alive) return;
        setDetail(d);
        setCoreQuestion(d.core_question);
        setBody(d.body);
      })
      .catch((e) => {
        if (alive) setError(e instanceof AuthorError ? e.message : "불러오기 실패");
      });
    return () => {
      alive = false;
    };
  }, [agentId, conceptId]);

  async function save() {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      await updateConcept(agentId, conceptId, { core_question: coreQuestion, body });
      onSaved();
    } catch (e) {
      setError(e instanceof AuthorError ? e.message : "저장 실패");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="rounded-md border border-[var(--ds-color-primary)] bg-[var(--ds-color-surface-tint)] p-ds-12">
      <div className="mb-ds-8 flex items-center gap-ds-8">
        <Pencil aria-hidden className="h-4 w-4 text-[var(--ds-color-primary)]" />
        <span className="font-mono text-xs text-[var(--ds-color-ink-muted)]">{conceptId}</span>
        {detail && <Tag tone="neutral">{detail.domain}</Tag>}
        <button type="button" onClick={onClose} aria-label="편집 닫기" className="ml-auto text-[var(--ds-color-ink-subtle)] hover:text-[var(--ds-color-ink)]">
          <X aria-hidden className="h-4 w-4" />
        </button>
      </div>
      {error && (
        <p role="alert" className="mb-ds-8 text-xs text-[var(--ds-color-danger)]">{error}</p>
      )}
      {!detail && !error ? (
        <p className="text-sm text-[var(--ds-color-ink-subtle)]">불러오는 중…</p>
      ) : (
        <div className="flex flex-col gap-ds-8">
          <label className="text-xs font-medium text-[var(--ds-color-ink-subtle)]">core_question</label>
          <input
            value={coreQuestion}
            onChange={(e) => setCoreQuestion(e.target.value)}
            className="rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)] px-ds-8 py-[6px] text-sm text-[var(--ds-color-ink)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ds-color-primary)]"
          />
          <label className="text-xs font-medium text-[var(--ds-color-ink-subtle)]">본문</label>
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={4}
            className="ds-scrollbar-thin resize-y rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)] px-ds-8 py-[6px] text-sm text-[var(--ds-color-ink)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ds-color-primary)]"
          />
          <div className="flex items-center justify-end gap-ds-8">
            <Button size="sm" variant="secondary" onClick={onClose} disabled={saving}>취소</Button>
            <Button size="sm" variant="primary" onClick={() => void save()} loading={saving} disabled={saving || !coreQuestion.trim() || !body.trim()}>
              저장 · 재배포
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
