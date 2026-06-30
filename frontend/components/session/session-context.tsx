"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  postLogin,
  postLogout,
  identityFor,
  type DemoIdentity,
} from "@/lib/session-api";

// Client-side memory of the chosen demo identity. The *real* auth is the
// httponly aon_session cookie set by POST /login; this context only remembers
// which user_id we picked so the shell can show "현재 신원" and gate /inbox·
// /console with a "로그인 필요" state. Cleared on logout.
const STORAGE_KEY = "aon.operator.userId";

interface SessionState {
  userId: string | null;
  identity: DemoIdentity | undefined;
  ready: boolean; // hydrated from storage (avoids SSR/client flash)
  pending: boolean;
  error: string | null;
  login: (userId: string) => Promise<void>;
  logout: () => Promise<void>;
}

const SessionCtx = createContext<SessionState | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [userId, setUserId] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY);
      if (stored) setUserId(stored);
    } catch {
      // localStorage unavailable — stay logged out.
    }
    setReady(true);
  }, []);

  const login = useCallback(async (next: string) => {
    setPending(true);
    setError(null);
    try {
      const confirmed = await postLogin(next);
      setUserId(confirmed);
      try {
        window.localStorage.setItem(STORAGE_KEY, confirmed);
      } catch {
        /* ignore */
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : "로그인에 실패했습니다.";
      setError(msg);
      throw e;
    } finally {
      setPending(false);
    }
  }, []);

  const logout = useCallback(async () => {
    setPending(true);
    try {
      await postLogout();
    } finally {
      setUserId(null);
      setError(null);
      try {
        window.localStorage.removeItem(STORAGE_KEY);
      } catch {
        /* ignore */
      }
      setPending(false);
    }
  }, []);

  const value = useMemo<SessionState>(
    () => ({
      userId,
      identity: identityFor(userId),
      ready,
      pending,
      error,
      login,
      logout,
    }),
    [userId, ready, pending, error, login, logout],
  );

  return <SessionCtx.Provider value={value}>{children}</SessionCtx.Provider>;
}

export function useSession(): SessionState {
  const ctx = useContext(SessionCtx);
  if (ctx == null) {
    throw new Error("useSession must be used within <SessionProvider>");
  }
  return ctx;
}
