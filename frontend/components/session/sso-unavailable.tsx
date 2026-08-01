import { LockKeyhole } from "lucide-react";

/**
 * RB3.2b.1 Central fallback while RB3.2b.2 OIDC is not assembled.
 * It intentionally offers no identity selector, client-side identity cache, or
 * Developer Reference login path.
 */
export function SsoUnavailable({ surface }: { surface: string }) {
  return (
    <div className="px-ds-16 py-ds-24 md:px-ds-24">
      <section className="mx-auto max-w-xl rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-24">
        <div className="flex items-start gap-ds-12">
          <LockKeyhole aria-hidden className="mt-1 h-5 w-5 shrink-0 text-[var(--ds-color-ink-subtle)]" />
          <div>
            <h2 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
              SSO 연결 준비 중
            </h2>
            <p className="mt-ds-4 text-sm text-[var(--ds-color-ink-muted)]">
              {surface}은(는) Central OIDC 세션이 준비된 뒤에만 사용할 수 있습니다.
              현재는 신원을 선택하거나 입력해 접근할 수 없습니다.
            </p>
          </div>
        </div>
      </section>
    </div>
  );
}
