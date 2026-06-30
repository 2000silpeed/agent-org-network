"use client";

import { type ReactNode } from "react";
import { LogIn, ShieldUser, UserCircle2 } from "lucide-react";
import { DEMO_IDENTITIES, type DemoRole } from "@/lib/session-api";
import { useSession } from "./session-context";

// Gate for operational surfaces (/inbox·/console). Until a demo identity is
// chosen (session cookie pinned via POST /login), the backend returns 401 and
// we show a "로그인 필요" state with a one-click identity picker. `requiredRole`
// is advisory — it pre-highlights the identities that surface makes sense for,
// but the backend is the real authority (e.g. /inbox needs an owner with cases).
export function LoginGate({
  surface,
  requiredRole,
  children,
}: {
  surface: string;
  requiredRole?: DemoRole;
  children: ReactNode;
}) {
  const { userId, ready, pending, login } = useSession();

  if (!ready) {
    return (
      <div className="px-ds-16 py-ds-24 text-sm text-[var(--ds-color-ink-subtle)] md:px-ds-24">
        신원 확인 중…
      </div>
    );
  }

  if (userId) return <>{children}</>;

  const suggested = requiredRole
    ? DEMO_IDENTITIES.filter((d) => d.role === requiredRole)
    : DEMO_IDENTITIES;

  return (
    <div className="px-ds-16 py-ds-24 md:px-ds-24">
      <div className="mx-auto max-w-xl rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-24">
        <div className="flex items-center gap-ds-12">
          <span className="flex h-10 w-10 items-center justify-center rounded-md bg-[var(--ds-color-surface-tint)]">
            <LogIn aria-hidden className="h-5 w-5 text-[var(--ds-color-primary)]" />
          </span>
          <div>
            <h2 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
              로그인 필요
            </h2>
            <p className="text-sm text-[var(--ds-color-ink-muted)]">
              {surface}은(는) 운영 신원으로 접근하는 면입니다. 데모 신원을 선택하세요.
            </p>
          </div>
        </div>

        <ul className="mt-ds-16 flex flex-col gap-ds-8">
          {suggested.map((d) => (
            <li key={d.userId}>
              <button
                type="button"
                onClick={() => void login(d.userId).catch(() => undefined)}
                disabled={pending}
                className="flex w-full items-center gap-ds-12 rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-12 py-ds-8 text-left transition-colors duration-ds-fast hover:bg-[var(--ds-color-surface-muted)] disabled:opacity-60"
              >
                {d.role === "operator" ? (
                  <ShieldUser aria-hidden className="h-[18px] w-[18px] shrink-0 text-[var(--ds-color-primary)]" />
                ) : (
                  <UserCircle2 aria-hidden className="h-[18px] w-[18px] shrink-0 text-[var(--ds-color-success)]" />
                )}
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium text-[var(--ds-color-ink)]">
                    {d.label}
                    <span className="ml-ds-8 text-xs font-normal text-[var(--ds-color-ink-subtle)]">
                      {d.role === "operator" ? "운영자" : "Owner"}
                    </span>
                  </span>
                  <span className="block truncate text-xs text-[var(--ds-color-ink-subtle)]">
                    {d.domainHint}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>

        {requiredRole && (
          <p className="mt-ds-12 text-xs text-[var(--ds-color-ink-subtle)]">
            상단 신원 드롭다운에서 다른 신원으로도 전환할 수 있습니다.
          </p>
        )}
      </div>
    </div>
  );
}
