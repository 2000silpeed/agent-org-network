"use client";

import Link from "next/link";
import Image from "next/image";
import { usePathname } from "next/navigation";
import { ShieldCheck } from "lucide-react";
import { NAV_ITEMS } from "./nav";
import { IdentitySwitcher } from "@/components/session/identity-switcher";
import { cn } from "@/lib/utils";

// sidebar-nav (navigation family): container, nav-item, icon, label, indicator(active).
// Active state is conveyed by surface + left indicator bar + aria-current — not color alone.
export function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className="hidden w-[244px] shrink-0 flex-col border-r border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] lg:flex">
      <div className="flex items-center gap-ds-8 border-b border-[var(--ds-color-border)] px-ds-16 py-ds-16">
        <span className="flex h-9 w-9 items-center justify-center rounded-md bg-[var(--ds-color-surface-tint)]">
          <Image
            src="/brand/mark.png"
            alt="Agent Org Network"
            width={28}
            height={28}
            priority
            className="h-7 w-7"
          />
        </span>
        <div className="min-w-0">
          <p className="truncate font-heading text-sm font-semibold text-[var(--ds-color-ink)]">
            Agent Org Network
          </p>
          <p className="truncate text-xs text-[var(--ds-color-ink-subtle)]">
            question-routing console
          </p>
        </div>
      </div>

      <nav aria-label="제품 영역" className="flex-1 px-ds-8 py-ds-12">
        <ul className="flex flex-col gap-ds-2">
          {NAV_ITEMS.map((item) => {
            const active =
              pathname === item.href || pathname.startsWith(item.href + "/");
            const Icon = item.icon;
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "group relative flex items-start gap-ds-12 rounded-md px-ds-12 py-ds-8 transition-colors duration-ds-fast ease-ds-standard",
                    active
                      ? "bg-[var(--ds-color-surface-muted)]"
                      : "hover:bg-[var(--ds-color-surface-muted)]"
                  )}
                >
                  {active && (
                    <span
                      aria-hidden
                      className="absolute left-0 top-1/2 h-6 w-[3px] -translate-y-1/2 rounded-pill bg-[var(--ds-color-primary)]"
                    />
                  )}
                  <Icon
                    aria-hidden
                    className={cn(
                      "mt-[2px] h-[18px] w-[18px] shrink-0",
                      active
                        ? "text-[var(--ds-color-primary)]"
                        : "text-[var(--ds-color-ink-subtle)] group-hover:text-[var(--ds-color-ink-muted)]"
                    )}
                  />
                  <span className="min-w-0">
                    <span
                      className={cn(
                        "block text-sm font-medium",
                        active
                          ? "text-[var(--ds-color-ink)]"
                          : "text-[var(--ds-color-ink-muted)] group-hover:text-[var(--ds-color-ink)]"
                      )}
                    >
                      {item.label}
                    </span>
                    <span className="block truncate text-xs text-[var(--ds-color-ink-subtle)]">
                      {item.description}
                    </span>
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      <div className="flex flex-col gap-ds-12 border-t border-[var(--ds-color-border)] px-ds-16 py-ds-12">
        <IdentitySwitcher />
        <div className="flex items-center gap-ds-8 text-xs text-[var(--ds-color-ink-subtle)]">
          <ShieldCheck
            aria-hidden
            className="h-4 w-4 text-[var(--ds-color-success)]"
          />
          <span>권한은 중앙 routing_rules에서만 선언</span>
        </div>
      </div>
    </aside>
  );
}
