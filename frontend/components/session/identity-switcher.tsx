"use client";

import { useEffect, useRef, useState } from "react";
import { UserCircle2, ChevronDown, LogOut, Check, ShieldUser } from "lucide-react";
import { DEMO_IDENTITIES } from "@/lib/session-api";
import { useSession } from "./session-context";
import { StatusBadge } from "@/components/ui/status-badge";
import { cn } from "@/lib/utils";

// Demo identity picker (운영/owner 세션). Passwordless — choosing a user pins it
// into the session cookie via POST /login. SSO is a follow-up (실 SSO 후속).
export function IdentitySwitcher({
  placement = "down",
}: {
  // "up" opens the menu above the trigger — use in the sidebar footer (bottom-anchored)
  // so the 6-identity list doesn't overflow below the viewport.
  placement?: "up" | "down";
} = {}) {
  const { userId, identity, ready, pending, error, login, logout } = useSession();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onEsc(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);

  // Avoid hydration flash: render a stable placeholder until storage is read.
  const labelText = !ready
    ? "신원 확인 중…"
    : identity
      ? identity.label
      : "신원 선택";

  async function choose(next: string) {
    if (next === userId) {
      setOpen(false);
      return;
    }
    try {
      await login(next);
      setOpen(false);
    } catch {
      // error surfaced inline below; keep the menu open.
    }
  }

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        disabled={pending}
        className={cn(
          "inline-flex max-w-full items-center gap-ds-8 rounded-pill border px-ds-12 py-[6px] text-sm font-medium transition-colors duration-ds-fast",
          "border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] text-[var(--ds-color-ink)]",
          "hover:bg-[var(--ds-color-surface-muted)] disabled:opacity-60",
        )}
      >
        {identity?.role === "operator" ? (
          <ShieldUser aria-hidden className="h-4 w-4 shrink-0 text-[var(--ds-color-primary)]" />
        ) : (
          <UserCircle2
            aria-hidden
            className={cn(
              "h-4 w-4 shrink-0",
              identity ? "text-[var(--ds-color-success)]" : "text-[var(--ds-color-ink-subtle)]",
            )}
          />
        )}
        <span className="truncate">{labelText}</span>
        {identity && (
          <span className="hidden text-xs font-normal text-[var(--ds-color-ink-subtle)] sm:inline">
            {identity.role === "operator" ? "운영자" : "Owner"}
          </span>
        )}
        <ChevronDown
          aria-hidden
          className={cn(
            "h-4 w-4 shrink-0 text-[var(--ds-color-ink-subtle)] transition-transform",
            open && "rotate-180",
          )}
        />
      </button>

      {open && (
        <div
          role="menu"
          aria-label="데모 신원 선택"
          className={cn(
            "absolute z-30 max-w-[calc(100vw-2rem)] overflow-hidden rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface-elevated)] shadow-xl",
            // up (sidebar footer): align left, fit within the 244px sidebar column so it
            // doesn't overhang into content. down (topbar, right-anchored): wider, align right.
            placement === "up"
              ? "bottom-full mb-ds-8 left-0 w-[212px]"
              : "top-full mt-ds-8 right-0 w-[260px]",
          )}
        >
          <div className="border-b border-[var(--ds-color-border)] px-ds-12 py-ds-8">
            <p className="text-xs font-semibold text-[var(--ds-color-ink-muted)]">
              데모 신원 선택
            </p>
            <p className="mt-ds-2 text-[11px] text-[var(--ds-color-ink-subtle)]">
              무비밀번호 — 선택 시 세션에 고정됩니다. (실 SSO 후속)
            </p>
          </div>

          <ul className="max-h-[320px] overflow-y-auto py-ds-4">
            {DEMO_IDENTITIES.map((d) => {
              const selected = d.userId === userId;
              return (
                <li key={d.userId}>
                  <button
                    type="button"
                    role="menuitemradio"
                    aria-checked={selected}
                    onClick={() => void choose(d.userId)}
                    disabled={pending}
                    className={cn(
                      "flex w-full items-start gap-ds-8 px-ds-12 py-ds-8 text-left transition-colors duration-ds-fast",
                      "hover:bg-[var(--ds-color-surface-muted)] disabled:opacity-60",
                      selected && "bg-[var(--ds-color-surface-tint)]",
                    )}
                  >
                    <span className="mt-[2px] w-4 shrink-0">
                      {selected && (
                        <Check aria-hidden className="h-4 w-4 text-[var(--ds-color-primary)]" />
                      )}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-ds-8">
                        <span className="truncate text-sm font-medium text-[var(--ds-color-ink)]">
                          {d.label}
                        </span>
                        <StatusBadge
                          tone={d.role === "operator" ? "info" : "neutral"}
                          label={d.role === "operator" ? "운영자" : "Owner"}
                        />
                      </span>
                      <span className="mt-ds-2 block truncate text-xs text-[var(--ds-color-ink-subtle)]">
                        {d.domainHint}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>

          {error && (
            <p className="border-t border-[var(--ds-color-border)] px-ds-12 py-ds-8 text-xs text-[var(--ds-color-danger)]">
              {error}
            </p>
          )}

          {userId && (
            <div className="border-t border-[var(--ds-color-border)] p-ds-8">
              <button
                type="button"
                onClick={() => void logout()}
                disabled={pending}
                className="flex w-full items-center gap-ds-8 rounded-md px-ds-12 py-ds-8 text-sm text-[var(--ds-color-ink-muted)] transition-colors duration-ds-fast hover:bg-[var(--ds-color-surface-muted)] hover:text-[var(--ds-color-ink)] disabled:opacity-60"
              >
                <LogOut aria-hidden className="h-4 w-4" />
                로그아웃
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
