import * as React from "react";
import { cn } from "@/lib/utils";

// data-display / tag — classification & label chip. Neutral by default; one
// optional semantic tone may alias a single --ds-* role (never recombine roles).
export type TagTone = "neutral" | "info" | "success" | "warning" | "danger";

const toneStyle: Record<TagTone, React.CSSProperties> = {
  neutral: {
    background: "var(--ds-color-surface-muted)",
    color: "var(--ds-color-ink-muted)",
    borderColor: "var(--ds-color-border)",
  },
  info: {
    background: "color-mix(in srgb, var(--ds-color-info) 12%, transparent)",
    color: "var(--ds-color-info)",
    borderColor: "color-mix(in srgb, var(--ds-color-info) 35%, transparent)",
  },
  success: {
    background: "color-mix(in srgb, var(--ds-color-success) 12%, transparent)",
    color: "var(--ds-color-success)",
    borderColor: "color-mix(in srgb, var(--ds-color-success) 35%, transparent)",
  },
  warning: {
    background: "color-mix(in srgb, var(--ds-color-warning) 16%, transparent)",
    color: "var(--ds-color-warning)",
    borderColor: "color-mix(in srgb, var(--ds-color-warning) 35%, transparent)",
  },
  danger: {
    background: "color-mix(in srgb, var(--ds-color-danger) 12%, transparent)",
    color: "var(--ds-color-danger)",
    borderColor: "color-mix(in srgb, var(--ds-color-danger) 35%, transparent)",
  },
};

export interface TagProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: TagTone;
  leadingIcon?: React.ReactNode;
}

export function Tag({
  tone = "neutral",
  leadingIcon,
  className,
  children,
  ...props
}: TagProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-ds-4 rounded-pill border px-ds-8 py-[2px] text-xs font-medium",
        className
      )}
      style={toneStyle[tone]}
      {...props}
    >
      {leadingIcon}
      {children}
    </span>
  );
}
