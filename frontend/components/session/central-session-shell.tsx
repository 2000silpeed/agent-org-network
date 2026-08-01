"use client";

import { useEffect, useState } from "react";
import { LogIn, LogOut, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";

type BrowserSessionProjection = {
  authenticated: true;
  registry_user_ref: string;
  expires_at: string;
  actions: string[];
};

type SessionState = "loading" | "anonymous" | "authenticated" | "unavailable";

const CSRF_COOKIE = "__Host-aon-central-csrf";

function csrfCookie(): string | null {
  try {
    const prefix = `${CSRF_COOKIE}=`;
    const item = document.cookie.split(";").map((value) => value.trim()).find((value) => value.startsWith(prefix));
    return item ? decodeURIComponent(item.slice(prefix.length)) : null;
  } catch {
    return null;
  }
}

export function CentralSessionShell(): JSX.Element {
  const [state, setState] = useState<SessionState>("loading");

  useEffect(() => {
    let active = true;
    void fetch("/api/auth/session", { credentials: "same-origin", cache: "no-store" }).then(async (response) => {
      if (!active) return;
      if (response.status === 401) {
        setState("anonymous");
        return;
      }
      if (!response.ok) {
        setState("unavailable");
        return;
      }
      const value: unknown = await response.json();
      if (!isBrowserSessionProjection(value)) {
        setState("unavailable");
        return;
      }
      setState("authenticated");
    }).catch(() => {
      if (active) setState("unavailable");
    });
    return () => { active = false; };
  }, []);

  if (state === "loading") return <div aria-live="polite" className="text-xs text-[var(--ds-color-ink-subtle)]">세션 확인 중</div>;
  if (state === "anonymous") return <CentralLoginForm />;
  if (state === "unavailable") return <div className="text-xs text-[var(--ds-color-ink-subtle)]">SSO 세션을 확인할 수 없습니다.</div>;
  return <CentralLogout onComplete={() => setState("anonymous")} />;
}

function CentralLoginForm(): JSX.Element {
  return (
    <form method="post" action="/api/auth/login/start">
      <Button type="submit" size="sm">
        <LogIn aria-hidden className="mr-ds-4 h-4 w-4" />
        조직 SSO 로그인
      </Button>
    </form>
  );
}

function CentralLogout({ onComplete }: { onComplete: () => void }): JSX.Element {
  const [pending, setPending] = useState(false);

  async function logout(): Promise<void> {
    const csrf = csrfCookie();
    if (csrf === null || pending) return;
    setPending(true);
    try {
      const response = await fetch("/api/auth/logout", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "X-AON-CSRF": csrf },
      });
      if (response.status === 204) onComplete();
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="flex items-center gap-ds-8">
      <span className="inline-flex items-center gap-ds-4 text-xs text-[var(--ds-color-ink-muted)]">
        <ShieldCheck aria-hidden className="h-4 w-4" /> 조직 SSO 연결됨
      </span>
      <Button type="button" variant="secondary" size="sm" disabled={pending} onClick={() => void logout()}>
        <LogOut aria-hidden className="mr-ds-4 h-4 w-4" /> 로그아웃
      </Button>
    </div>
  );
}

function isBrowserSessionProjection(value: unknown): value is BrowserSessionProjection {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return Object.keys(candidate).sort().join(",") === "actions,authenticated,expires_at,registry_user_ref" &&
    candidate.authenticated === true && typeof candidate.registry_user_ref === "string" && candidate.registry_user_ref.length > 0 &&
    typeof candidate.expires_at === "string" && !Number.isNaN(Date.parse(candidate.expires_at)) &&
    Array.isArray(candidate.actions) && candidate.actions.length === 1 && candidate.actions[0] === "session.read";
}
