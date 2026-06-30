"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { Sun, Moon } from "lucide-react";

// Light/dark theme. The design system ships both token sets (data-theme on <html>).
// A no-flash inline script in layout.tsx sets data-theme from localStorage before
// paint; this provider mirrors it into React state so the toggle + theme-aware
// assets (illustrations) can react.

type Theme = "dark" | "light";
const STORAGE_KEY = "aon.theme";

const ThemeContext = createContext<{ theme: Theme; toggle: () => void } | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>("dark");

  // Read the value the no-flash script already applied to <html> (avoids a flash).
  useEffect(() => {
    const current = document.documentElement.getAttribute("data-theme");
    setTheme(current === "light" ? "light" : "dark");
  }, []);

  const toggle = useCallback(() => {
    setTheme((prev) => {
      const next: Theme = prev === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try {
        localStorage.setItem(STORAGE_KEY, next);
      } catch {
        /* storage blocked — session-only theme is fine */
      }
      return next;
    });
  }, []);

  return (
    <ThemeContext.Provider value={{ theme, toggle }}>{children}</ThemeContext.Provider>
  );
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used within <ThemeProvider>");
  return ctx;
}

export function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const toLight = theme === "dark";
  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={toLight ? "라이트 모드로 전환" : "다크 모드로 전환"}
      title={toLight ? "라이트 모드" : "다크 모드"}
      className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] text-[var(--ds-color-ink-muted)] transition-colors duration-ds-fast hover:bg-[var(--ds-color-surface-muted)] hover:text-[var(--ds-color-ink)]"
    >
      {toLight ? (
        <Sun aria-hidden className="h-4 w-4" />
      ) : (
        <Moon aria-hidden className="h-4 w-4" />
      )}
    </button>
  );
}
