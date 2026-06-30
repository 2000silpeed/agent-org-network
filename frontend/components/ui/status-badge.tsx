import * as React from "react";
import {
  CheckCircle2,
  Info,
  AlertTriangle,
  XCircle,
  Clock,
  CircleDot,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

// Non-color status: status is NEVER conveyed by color alone.
// Every badge pairs an icon + a word. Color is a secondary reinforcement only.
export type StatusTone =
  | "info"
  | "success"
  | "warning"
  | "danger"
  | "neutral"
  | "pending";

const toneMap: Record<
  StatusTone,
  { icon: LucideIcon; color: string; surface: string; role: "status" | "alert" }
> = {
  info: {
    icon: Info,
    color: "var(--ds-color-info)",
    surface: "color-mix(in srgb, var(--ds-color-info) 14%, transparent)",
    role: "status",
  },
  success: {
    icon: CheckCircle2,
    color: "var(--ds-color-success)",
    surface: "color-mix(in srgb, var(--ds-color-success) 14%, transparent)",
    role: "status",
  },
  warning: {
    icon: AlertTriangle,
    color: "var(--ds-color-warning)",
    surface: "color-mix(in srgb, var(--ds-color-warning) 16%, transparent)",
    role: "status",
  },
  danger: {
    icon: XCircle,
    color: "var(--ds-color-danger)",
    surface: "color-mix(in srgb, var(--ds-color-danger) 14%, transparent)",
    role: "alert",
  },
  pending: {
    icon: Clock,
    color: "var(--ds-color-ink-subtle)",
    surface: "var(--ds-color-surface-muted)",
    role: "status",
  },
  neutral: {
    icon: CircleDot,
    color: "var(--ds-color-ink-muted)",
    surface: "var(--ds-color-surface-muted)",
    role: "status",
  },
};

export interface StatusBadgeProps {
  tone: StatusTone;
  label: string;
  /** Override the default tone icon when the semantics need a specific glyph. */
  icon?: LucideIcon;
  className?: string;
}

export function StatusBadge({ tone, label, icon, className }: StatusBadgeProps) {
  const t = toneMap[tone];
  const Icon = icon ?? t.icon;
  return (
    <span
      role={t.role}
      className={cn(
        "inline-flex items-center gap-ds-4 rounded-sm px-ds-8 py-[3px] text-xs font-medium",
        className
      )}
      style={{ background: t.surface, color: t.color }}
    >
      <Icon aria-hidden className="h-[13px] w-[13px] shrink-0" />
      <span className="text-[var(--ds-color-ink)]">{label}</span>
    </span>
  );
}
