import { ArrowRight } from "lucide-react";

// diff-viewer / redline-viewer: original strike-through → revised.
// Used when a reviewer edits core_question so the change is auditable before publish.
export function DiffViewer({
  original,
  revised,
  label = "core_question 수정",
}: {
  original: string;
  revised: string;
  label?: string;
}) {
  return (
    <div className="rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)] p-ds-12">
      <p className="mb-ds-8 text-xs font-medium text-[var(--ds-color-ink-subtle)]">
        {label}
      </p>
      <div className="flex flex-col gap-ds-4 text-sm">
        <del className="text-[var(--ds-color-danger)] line-through decoration-[var(--ds-color-danger)]/60">
          {original}
        </del>
        <div className="flex items-start gap-ds-4">
          <ArrowRight
            aria-hidden
            className="mt-[3px] h-[14px] w-[14px] shrink-0 text-[var(--ds-color-success)]"
          />
          <ins className="text-[var(--ds-color-ink)] no-underline">
            {revised}
          </ins>
        </div>
      </div>
    </div>
  );
}
