import {
  ShieldCheck,
  Laptop,
  FileText,
  Rocket,
  GitCommitHorizontal,
} from "lucide-react";
import { PageHeader } from "@/components/app-shell/page-header";
import { Button } from "@/components/ui/button";
import { StepProgress } from "@/components/ui/step-progress";
import { ConceptCard } from "@/components/author/concept-card";
import { authorSteps, conceptDrafts } from "@/lib/mock-data";

export default function AuthorPage() {
  const approved = conceptDrafts.filter(
    (c) => c.disposition !== "rejected"
  ).length;
  const edited = conceptDrafts.filter((c) => c.disposition === "edited").length;
  const rejected = conceptDrafts.filter(
    (c) => c.disposition === "rejected"
  ).length;

  return (
    <div className="flex flex-col">
      <PageHeader
        surface="Author"
        persona="Owner"
        title="지식 저작 (OKF)"
        description="원본 문서를 개념 단위로 분할하고, core_question을 다듬어 승인된 목차만 중앙에 배포합니다."
        actions={
          <div className="flex flex-wrap items-center gap-ds-8 text-xs">
            <span className="inline-flex items-center gap-ds-4 rounded-pill border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-8 py-[4px] text-[var(--ds-color-ink-muted)]">
              <Laptop
                aria-hidden
                className="h-[14px] w-[14px] text-[var(--ds-color-info)]"
              />
              내 환경 실행
            </span>
            <span className="inline-flex items-center gap-ds-4 rounded-pill border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-8 py-[4px] text-[var(--ds-color-ink-muted)]">
              raw·초안은 로컬
            </span>
            <span className="inline-flex items-center gap-ds-4 rounded-pill border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-8 py-[4px] text-[var(--ds-color-ink-muted)]">
              <ShieldCheck
                aria-hidden
                className="h-[14px] w-[14px] text-[var(--ds-color-success)]"
              />
              중앙은 승인된 목차만
            </span>
          </div>
        }
      />

      <div className="flex flex-col gap-ds-16 px-ds-16 py-ds-16 md:px-ds-24">
        {/* staged stepper */}
        <div className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-16 py-ds-12">
          <StepProgress steps={authorSteps} />
        </div>

        {/* source summary strip */}
        <div className="flex items-start gap-ds-12 rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface-muted)] px-ds-16 py-ds-12">
          <FileText
            aria-hidden
            className="mt-[2px] h-5 w-5 shrink-0 text-[var(--ds-color-ink-subtle)]"
          />
          <div className="min-w-0">
            <p className="text-sm font-medium text-[var(--ds-color-ink)]">
              원본: 2026년 인사·복리후생 규정 개정안.pdf
            </p>
            <p className="mt-ds-2 text-xs text-[var(--ds-color-ink-muted)]">
              18페이지 · 개념 분할 결과 3건 추출 · raw 문서는 로컬에만 보관됩니다.
            </p>
          </div>
        </div>

        {/* concept drafts */}
        <div>
          <h2 className="mb-ds-12 font-heading text-md font-semibold text-[var(--ds-color-ink)]">
            개념 초안 (3)
          </h2>
          <div className="grid grid-cols-1 gap-ds-16 lg:grid-cols-2 xl:grid-cols-3">
            {conceptDrafts.map((draft) => (
              <ConceptCard key={draft.id} draft={draft} />
            ))}
          </div>
        </div>
      </div>

      {/* disposition summary + commit/deploy bar */}
      <div className="sticky bottom-0 z-10 mt-auto border-t border-[var(--ds-color-border)] bg-[var(--ds-color-surface)]/95 px-ds-16 py-ds-12 backdrop-blur md:px-ds-24">
        <div className="flex flex-col gap-ds-12 md:flex-row md:items-center md:justify-between">
          <p className="text-sm text-[var(--ds-color-ink-muted)]">
            <span className="font-medium text-[var(--ds-color-ink)]">
              승인 {approved} · 수정 {edited} · 거부 {rejected}
            </span>{" "}
            — 거부분은 배포되지 않습니다.
          </p>
          <div className="flex flex-wrap items-center gap-ds-8">
            <Button variant="secondary" size="md">
              <GitCommitHorizontal aria-hidden className="h-4 w-4" />
              커밋
            </Button>
            <Button variant="primary" size="md">
              <Rocket aria-hidden className="h-4 w-4" />
              배포
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
