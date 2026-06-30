import * as React from "react";

export function PageHeader({
  surface,
  persona,
  title,
  description,
  actions,
}: {
  surface: string;
  persona: string;
  title: string;
  description: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-ds-12 border-b border-[var(--ds-color-border)] px-ds-16 py-ds-16 md:flex-row md:items-end md:justify-between md:px-ds-24">
      <div className="min-w-0">
        <div className="flex items-center gap-ds-8 text-xs font-medium uppercase tracking-wide text-[var(--ds-color-ink-subtle)]">
          <span>{surface}</span>
          <span aria-hidden>·</span>
          <span>{persona}</span>
        </div>
        <h1 className="mt-ds-4 font-heading text-xl font-semibold text-[var(--ds-color-ink)]">
          {title}
        </h1>
        <p className="mt-ds-4 max-w-2xl text-sm text-[var(--ds-color-ink-muted)]">
          {description}
        </p>
      </div>
      {actions && (
        <div className="flex flex-wrap items-center gap-ds-8">{actions}</div>
      )}
    </div>
  );
}
