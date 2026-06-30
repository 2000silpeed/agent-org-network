"use client";

import { Check, Pencil, Ban, Lock } from "lucide-react";
import type { ConceptDraft } from "@/lib/mock-data";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/card";
import { Tag } from "@/components/ui/tag";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { DiffViewer } from "./diff-viewer";

const dispositionMeta = {
  unreviewed: { tone: "pending" as const, label: "미검토" },
  edited: { tone: "warning" as const, label: "수정됨" },
  rejected: { tone: "danger" as const, label: "거부" },
};

// decision-record-card + approval-rail. Each concept draft carries its own
// disposition (approve / edit / reject). Rejected drafts are dimmed & excluded.
export function ConceptCard({ draft }: { draft: ConceptDraft }) {
  const meta = dispositionMeta[draft.disposition];
  const rejected = draft.disposition === "rejected";

  return (
    <Card
      elevated
      className={rejected ? "opacity-60" : undefined}
      aria-disabled={rejected || undefined}
    >
      <CardHeader>
        <div className="flex flex-wrap items-center gap-ds-8">
          <span className="font-mono text-xs text-[var(--ds-color-ink-subtle)]">
            {draft.conceptId}
          </span>
          <Tag tone={draft.inDomain ? "neutral" : "danger"}>
            {draft.domain}
            {!draft.inDomain && (
              <span className="text-[var(--ds-color-danger)]"> · 권한 밖</span>
            )}
          </Tag>
          <StatusBadge tone={meta.tone} label={meta.label} />
        </div>
        <h3 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
          {draft.title}
        </h3>
      </CardHeader>

      <CardBody className="flex flex-col gap-ds-12">
        <div>
          <p className="text-xs font-medium text-[var(--ds-color-ink-subtle)]">
            core_question
          </p>
          {draft.coreQuestionOriginal ? (
            <div className="mt-ds-8">
              <DiffViewer
                original={draft.coreQuestionOriginal}
                revised={draft.coreQuestion}
              />
            </div>
          ) : (
            <p className="mt-ds-4 text-sm text-[var(--ds-color-ink)]">
              {draft.coreQuestion}
            </p>
          )}
        </div>

        <div>
          <p className="text-xs font-medium text-[var(--ds-color-ink-subtle)]">
            body 미리보기
          </p>
          <p className="mt-ds-4 line-clamp-3 text-sm leading-normal text-[var(--ds-color-ink-muted)]">
            {draft.bodyPreview}
          </p>
        </div>

        {rejected && draft.rejectReason && (
          <div className="flex items-start gap-ds-8 rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface-muted)] p-ds-8 text-xs text-[var(--ds-color-ink-muted)]">
            <Lock
              aria-hidden
              className="mt-[1px] h-[14px] w-[14px] shrink-0 text-[var(--ds-color-danger)]"
            />
            <span>{draft.rejectReason}</span>
          </div>
        )}
      </CardBody>

      {/* approval-rail: approve / edit / reject */}
      <CardFooter>
        {rejected ? (
          <span className="text-xs text-[var(--ds-color-ink-subtle)]">
            이 개념은 배포에서 제외됩니다.
          </span>
        ) : (
          <>
            <Button size="sm" variant="success">
              <Check aria-hidden className="h-4 w-4" />
              승인
            </Button>
            <Button size="sm" variant="secondary">
              <Pencil aria-hidden className="h-4 w-4" />
              수정
            </Button>
            <Button size="sm" variant="danger">
              <Ban aria-hidden className="h-4 w-4" />
              거부
            </Button>
          </>
        )}
      </CardFooter>
    </Card>
  );
}
