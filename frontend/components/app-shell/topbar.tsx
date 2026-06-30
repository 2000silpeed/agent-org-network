"use client";

import Link from "next/link";
import Image from "next/image";
import { usePathname } from "next/navigation";
import { NAV_ITEMS } from "./nav";
import { IdentitySwitcher } from "@/components/session/identity-switcher";
import { ThemeToggle } from "./theme";
import { cn } from "@/lib/utils";

// Mobile/tablet primary nav. Sidebar is hidden below lg; this horizontal,
// wrap-safe rail carries the four-surface switch on narrow viewports.
export function Topbar() {
  const pathname = usePathname();
  return (
    <header className="sticky top-0 z-20 border-b border-[var(--ds-color-border)] bg-[var(--ds-color-surface)]/95 backdrop-blur lg:hidden">
      <div className="flex items-center gap-ds-8 px-ds-16 py-ds-12">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center overflow-hidden rounded-md bg-white shadow-sm ring-1 ring-[var(--ds-color-border)]">
          <Image
            src="/brand/mark.png"
            alt="Agent Org Network"
            width={28}
            height={28}
            priority
            className="h-7 w-7 object-contain"
          />
        </span>
        <span className="font-heading text-sm font-semibold text-[var(--ds-color-ink)]">
          Agent Org Network
        </span>
        <div className="ml-auto flex items-center gap-ds-8">
          <ThemeToggle />
          <IdentitySwitcher />
        </div>
      </div>
      <nav
        aria-label="제품 영역"
        className="ds-scrollbar-thin flex gap-ds-8 overflow-x-auto px-ds-12 pb-ds-8"
      >
        {NAV_ITEMS.map((item) => {
          const active =
            pathname === item.href || pathname.startsWith(item.href + "/");
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              className={cn(
                "inline-flex shrink-0 items-center gap-ds-4 rounded-pill border px-ds-12 py-[6px] text-sm font-medium transition-colors duration-ds-fast",
                active
                  ? "border-[var(--ds-color-primary)] bg-[var(--ds-color-surface-tint)] text-[var(--ds-color-ink)]"
                  : "border-[var(--ds-color-border)] text-[var(--ds-color-ink-muted)] hover:bg-[var(--ds-color-surface-muted)]"
              )}
            >
              <Icon aria-hidden className="h-4 w-4" />
              {item.label}
            </Link>
          );
        })}
      </nav>
    </header>
  );
}
