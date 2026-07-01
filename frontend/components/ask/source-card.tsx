"use client";

import { useState } from "react";
import { FileText, ChevronDown, Lock } from "lucide-react";
import type { SourceCard as SourceCardData } from "@/lib/mock-data";
import { Tag } from "@/components/ui/tag";

// source-card (copilot-artifact): repeatable citation preview + citation-drawer.
// Trust depends on showing which OKF concept the answer is grounded in.
export function SourceCard({ source }: { source: SourceCardData }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-ds-8 px-ds-12 py-ds-8 text-left transition-colors duration-ds-fast hover:bg-[var(--ds-color-surface-muted)]"
      >
        <FileText
          aria-hidden
          className="h-4 w-4 shrink-0 text-[var(--ds-color-ink-subtle)]"
        />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium text-[var(--ds-color-ink)]">
            {source.title}
          </span>
          <span className="block truncate font-mono text-xs text-[var(--ds-color-ink-subtle)]">
            {source.conceptId}
          </span>
        </span>
        <ChevronDown
          aria-hidden
          className={`h-4 w-4 shrink-0 text-[var(--ds-color-ink-subtle)] transition-transform duration-ds-fast ${
            open ? "rotate-180" : ""
          }`}
        />
      </button>
      {open && (
        <div className="border-t border-[var(--ds-color-border)] px-ds-12 py-ds-8">
          <div className="flex flex-wrap items-center gap-ds-8 text-xs">
            <Tag tone="neutral">{source.domain}</Tag>
            <span className="text-[var(--ds-color-ink-muted)]">
              담당 {source.owner}
            </span>
            <span aria-hidden className="text-[var(--ds-color-ink-subtle)]">
              ·
            </span>
            <span className="font-mono text-[var(--ds-color-ink-subtle)]">
              {source.conceptId}
            </span>
          </div>
          {/* 중앙은 비소유 — 원문(raw 문서·본문)은 담당자 환경에 있고 중앙은 출처·목차만 보관한다.
              그래서 익명 질문 화면에 "원문 열기"를 제공하지 않는다(노출/비소유 불변식). */}
          <p className="mt-ds-8 flex items-start gap-ds-4 text-xs text-[var(--ds-color-ink-subtle)]">
            <Lock aria-hidden className="mt-[2px] h-[12px] w-[12px] shrink-0" />
            원문은 담당자({source.owner}) 환경에 있습니다. 중앙은 출처와 목차만 보관합니다.
          </p>
        </div>
      )}
    </div>
  );
}
