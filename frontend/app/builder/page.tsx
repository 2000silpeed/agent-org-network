"use client";

import { useState } from "react";
import {
  IdCard,
  FileCode2,
  Copy,
  Check,
  GitPullRequestArrow,
  AlertTriangle,
  CheckCircle2,
  ShieldCheck,
} from "lucide-react";
import { PageHeader } from "@/components/app-shell/page-header";
import { LoginGate } from "@/components/session/login-gate";
import { useSession } from "@/components/session/session-context";
import { Button } from "@/components/ui/button";
import {
  validateCard,
  BuilderError,
  type BuilderCardInput,
} from "@/lib/builder-api";

// 카드를 폼으로 구성 → admission 검증(POST /builder/validate) → YAML 미리보기까지.
// 편집 채널은 git/PR — 라이브 레지스트리 쓰기 없음(CONTEXT Maintainer). 통과 YAML을
// Owner가 복사→커밋(PR)한다. owner 필드는 로그인 신원으로 고정(위조 차단, ADR 0016).
export default function BuilderPage() {
  return (
    <div className="flex flex-col">
      <PageHeader
        surface="Builder"
        persona="Owner"
        title="카드 빌더"
        description="자기 카드를 폼으로 구성해 검증합니다. 통과한 YAML을 registry/agents/에 커밋(PR)하면 실제로 등록됩니다 — 이 화면은 라이브 등록을 하지 않습니다."
      />
      <LoginGate surface="카드 빌더" requiredRole="owner">
        <BuilderWorkspace />
      </LoginGate>
    </div>
  );
}

// CSV 입력을 문자열 배열로(빈 항목 제거) — 옛 빌더 splitCsv와 동일.
function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

interface FormState {
  agentId: string;
  team: string;
  summary: string;
  domains: string;
  lastReviewedAt: string;
  maintainer: string;
  knowledgeSources: string;
  canAnswer: string;
  cannotAnswer: string;
  approvalWhen: string;
  collaborateWhen: string;
  trustLabels: string;
}

const EMPTY_FORM: FormState = {
  agentId: "",
  team: "",
  summary: "",
  domains: "",
  lastReviewedAt: todayIso(),
  maintainer: "",
  knowledgeSources: "",
  canAnswer: "",
  cannotAnswer: "",
  approvalWhen: "",
  collaborateWhen: "",
  trustLabels: "",
};

function BuilderWorkspace() {
  const { userId } = useSession();
  // owner는 세션 신원으로 고정한다 — 자기 카드만 구성(Owner 스코프). 백엔드도
  // 인증 ON이면 세션 ≠ owner를 403으로 막지만, UI에서 먼저 고정해 위조를 차단한다.
  const owner = userId ?? "";

  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [validating, setValidating] = useState(false);
  const [yaml, setYaml] = useState<string | null>(null);
  const [errors, setErrors] = useState<string[] | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  const canSubmit =
    !validating &&
    form.agentId.trim() !== "" &&
    form.team.trim() !== "" &&
    form.summary.trim() !== "" &&
    splitCsv(form.domains).length > 0;

  async function validate() {
    if (!canSubmit) return;
    setValidating(true);
    setYaml(null);
    setErrors(null);
    setRequestError(null);
    setCopied(false);

    const card: BuilderCardInput = {
      agent_id: form.agentId.trim(),
      owner,
      team: form.team.trim(),
      summary: form.summary.trim(),
      domains: splitCsv(form.domains),
      last_reviewed_at: form.lastReviewedAt.trim(),
      maintainer: form.maintainer.trim() || null,
      can_answer: splitCsv(form.canAnswer),
      cannot_answer: splitCsv(form.cannotAnswer),
      approval_when: splitCsv(form.approvalWhen),
      collaborate_when: splitCsv(form.collaborateWhen),
      knowledge_sources: splitCsv(form.knowledgeSources),
      trust_labels: splitCsv(form.trustLabels),
    };

    try {
      const result = await validateCard(card);
      if (result.ok) setYaml(result.yaml);
      else setErrors(result.errors.length > 0 ? result.errors : ["알 수 없는 오류"]);
    } catch (e) {
      setRequestError(
        e instanceof BuilderError ? e.message : "검증 요청에 실패했습니다.",
      );
    } finally {
      setValidating(false);
    }
  }

  async function copyYaml() {
    if (!yaml) return;
    try {
      await navigator.clipboard.writeText(yaml);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard unavailable — user can still select the block manually.
    }
  }

  return (
    <div className="flex flex-col gap-ds-16 px-ds-16 py-ds-16 md:px-ds-24">
      {/* 편집 채널 안내 — 라이브 등록이 아니라 git/PR 커밋임을 명확히 */}
      <div
        role="note"
        className="flex items-start gap-ds-8 rounded-md border border-[color-mix(in_srgb,var(--ds-color-info)_35%,transparent)] bg-[color-mix(in_srgb,var(--ds-color-info)_8%,transparent)] px-ds-16 py-ds-12 text-sm text-[var(--ds-color-ink-muted)]"
      >
        <GitPullRequestArrow
          aria-hidden
          className="mt-[2px] h-4 w-4 shrink-0 text-[var(--ds-color-info)]"
        />
        <p>
          이 빌더는 카드를{" "}
          <span className="font-medium text-[var(--ds-color-ink)]">검증·미리보기</span>만
          합니다. 통과한 YAML을 복사해{" "}
          <code className="rounded-sm bg-[var(--ds-color-surface-muted)] px-ds-4 py-[1px] font-mono text-xs text-[var(--ds-color-ink)]">
            registry/agents/{"{agent_id}"}.yaml
          </code>
          에 커밋(PR)해야 실제로 등록됩니다. 라이브 레지스트리에는 쓰지 않습니다.
        </p>
      </div>

      {/* 카드 구성 폼 */}
      <section className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] px-ds-16 py-ds-16">
        <div className="mb-ds-16 flex items-center gap-ds-8">
          <IdCard aria-hidden className="h-[18px] w-[18px] text-[var(--ds-color-ink-subtle)]" />
          <h2 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
            카드 구성
          </h2>
        </div>

        <div className="grid grid-cols-1 gap-ds-16 md:grid-cols-2">
          <Field label="agent_id" required hint="카드 식별자 — registry/agents/{agent_id}.yaml 파일명이 됩니다.">
            <TextInput
              value={form.agentId}
              onChange={(v) => set("agentId", v)}
              placeholder="예: new_ops"
            />
          </Field>

          {/* owner — 세션 신원으로 고정(편집 불가) */}
          <Field label="owner" hint="로그인 신원으로 고정됩니다 — 자기 카드만 구성할 수 있습니다.">
            <div className="flex h-10 items-center gap-ds-8 rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface-muted)] px-ds-12">
              <ShieldCheck aria-hidden className="h-4 w-4 shrink-0 text-[var(--ds-color-success)]" />
              <span className="truncate font-mono text-sm text-[var(--ds-color-ink)]">
                {owner || "—"}
              </span>
            </div>
          </Field>

          <Field label="team" required hint="담당 팀 — 라우팅·조직 그래프의 소속.">
            <TextInput
              value={form.team}
              onChange={(v) => set("team", v)}
              placeholder="예: cs"
            />
          </Field>

          <Field label="last_reviewed_at" required hint="마지막 검토일 (ISO date, YYYY-MM-DD).">
            <input
              type="date"
              value={form.lastReviewedAt}
              onChange={(e) => set("lastReviewedAt", e.target.value)}
              className="h-10 w-full rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)] px-ds-12 text-sm text-[var(--ds-color-ink)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ds-color-primary)]"
            />
          </Field>
        </div>

        <div className="mt-ds-16 flex flex-col gap-ds-16">
          <Field label="summary" required hint="이 카드가 담당하는 내용 한 줄 요약.">
            <textarea
              value={form.summary}
              onChange={(e) => set("summary", e.target.value)}
              rows={2}
              placeholder="예: 환불·보상 정책 문의 담당"
              className="ds-scrollbar-thin w-full resize-y rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)] px-ds-12 py-ds-8 text-sm text-[var(--ds-color-ink)] placeholder:text-[var(--ds-color-ink-subtle)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ds-color-primary)]"
            />
          </Field>

          <Field
            label="domains"
            required
            hint="쉼표로 구분 — 라우터가 이 도메인으로 질문을 배정합니다."
          >
            <TextInput
              value={form.domains}
              onChange={(v) => set("domains", v)}
              placeholder="쉼표로 구분: 환불, 보상"
            />
          </Field>
        </div>

        {/* 선택 필드 */}
        <div className="mt-ds-16 border-t border-[var(--ds-color-border)] pt-ds-16">
          <p className="mb-ds-12 text-xs font-medium uppercase tracking-wide text-[var(--ds-color-ink-subtle)]">
            선택 필드
          </p>
          <div className="grid grid-cols-1 gap-ds-16 md:grid-cols-2">
            <Field label="maintainer" hint="카드 유지관리자 user_id — Registry에 실재해야 합니다.">
              <TextInput
                value={form.maintainer}
                onChange={(v) => set("maintainer", v)}
                placeholder="예: root_manager"
              />
            </Field>
            <Field label="knowledge_sources" hint="쉼표로 구분 — 답변 근거가 되는 지식 출처.">
              <TextInput
                value={form.knowledgeSources}
                onChange={(v) => set("knowledgeSources", v)}
                placeholder="예: 위키/환불정책, Notion/가이드"
              />
            </Field>
            <Field label="can_answer" hint="쉼표로 구분 — 답변할 수 있는 범위.">
              <TextInput
                value={form.canAnswer}
                onChange={(v) => set("canAnswer", v)}
                placeholder="쉼표로 구분"
              />
            </Field>
            <Field label="cannot_answer" hint="쉼표로 구분 — 답변하지 않는 범위(경계).">
              <TextInput
                value={form.cannotAnswer}
                onChange={(v) => set("cannotAnswer", v)}
                placeholder="쉼표로 구분"
              />
            </Field>
            <Field label="approval_when" hint="쉼표로 구분 — 승인이 필요한 상황.">
              <TextInput
                value={form.approvalWhen}
                onChange={(v) => set("approvalWhen", v)}
                placeholder="쉼표로 구분"
              />
            </Field>
            <Field label="collaborate_when" hint="쉼표로 구분 — 협업이 필요한 상황.">
              <TextInput
                value={form.collaborateWhen}
                onChange={(v) => set("collaborateWhen", v)}
                placeholder="쉼표로 구분"
              />
            </Field>
            <Field label="trust_labels" hint="쉼표로 구분 — 신뢰 라벨(선택).">
              <TextInput
                value={form.trustLabels}
                onChange={(v) => set("trustLabels", v)}
                placeholder="쉼표로 구분"
              />
            </Field>
          </div>
        </div>

        <div className="mt-ds-16 flex justify-end">
          <Button
            variant="primary"
            size="md"
            onClick={() => void validate()}
            loading={validating}
            disabled={!canSubmit}
          >
            {!validating && <ShieldCheck aria-hidden className="h-4 w-4" />}
            검증 · YAML 미리보기
          </Button>
        </div>
      </section>

      {/* 요청 실패(네트워크·401·403) */}
      {requestError && (
        <div
          role="alert"
          className="flex items-center gap-ds-8 rounded-md border border-[var(--ds-color-danger)] bg-[color-mix(in_srgb,var(--ds-color-danger)_8%,transparent)] px-ds-12 py-ds-8 text-sm text-[var(--ds-color-ink)]"
        >
          <AlertTriangle aria-hidden className="h-4 w-4 shrink-0 text-[var(--ds-color-danger)]" />
          {requestError}
        </div>
      )}

      {/* 검증 실패 — errors 리스트 */}
      {errors && (
        <div className="rounded-lg border border-[var(--ds-color-danger)] bg-[var(--ds-color-surface)] px-ds-16 py-ds-12">
          <div className="mb-ds-8 flex items-center gap-ds-8">
            <AlertTriangle aria-hidden className="h-4 w-4 text-[var(--ds-color-danger)]" />
            <h3 className="text-sm font-semibold text-[var(--ds-color-ink)]">검증 실패</h3>
          </div>
          <ul className="flex flex-col gap-ds-4">
            {errors.map((err, i) => (
              <li
                key={i}
                className="flex items-start gap-ds-8 text-sm text-[var(--ds-color-ink-muted)]"
              >
                <span aria-hidden className="mt-[7px] h-1 w-1 shrink-0 rounded-pill bg-[var(--ds-color-danger)]" />
                {err}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 검증 통과 — YAML 미리보기 + 복사 + git/PR 안내 */}
      {yaml && (
        <div className="rounded-lg border border-[color-mix(in_srgb,var(--ds-color-success)_45%,transparent)] bg-[var(--ds-color-surface)] px-ds-16 py-ds-16">
          <div className="mb-ds-4 flex flex-wrap items-center gap-ds-8">
            <CheckCircle2 aria-hidden className="h-[18px] w-[18px] text-[var(--ds-color-success)]" />
            <h3 className="font-heading text-md font-semibold text-[var(--ds-color-ink)]">
              검증 통과 — YAML 미리보기
            </h3>
            <div className="ml-auto flex items-center gap-ds-8">
              {copied && (
                <span className="inline-flex items-center gap-ds-4 text-xs text-[var(--ds-color-success)]">
                  <Check aria-hidden className="h-[13px] w-[13px]" />
                  복사됨
                </span>
              )}
              <Button variant="secondary" size="sm" onClick={() => void copyYaml()}>
                <Copy aria-hidden className="h-4 w-4" />
                YAML 복사
              </Button>
            </div>
          </div>
          <p className="mb-ds-12 text-xs text-[var(--ds-color-ink-muted)]">
            아래 YAML을 복사해 커밋(PR)하면 등록됩니다. 이 화면은 라이브 등록을 하지 않습니다.
          </p>

          <div className="flex items-center gap-ds-8 border-b border-[var(--ds-color-border)] pb-ds-8">
            <FileCode2 aria-hidden className="h-4 w-4 text-[var(--ds-color-ink-subtle)]" />
            <span className="font-mono text-xs text-[var(--ds-color-ink-muted)]">
              registry/agents/{form.agentId.trim() || "{agent_id}"}.yaml
            </span>
          </div>
          <pre className="ds-scrollbar-thin mt-ds-8 max-h-[420px] overflow-auto rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)] px-ds-12 py-ds-12 font-mono text-xs leading-relaxed text-[var(--ds-color-ink)]">
            {yaml}
          </pre>

          <div className="mt-ds-12 rounded-md border border-[color-mix(in_srgb,var(--ds-color-success)_35%,transparent)] bg-[color-mix(in_srgb,var(--ds-color-success)_8%,transparent)] px-ds-16 py-ds-12 text-sm text-[var(--ds-color-ink-muted)]">
            <div className="mb-ds-8 flex items-center gap-ds-8">
              <GitPullRequestArrow aria-hidden className="h-4 w-4 shrink-0 text-[var(--ds-color-success)]" />
              <span className="font-medium text-[var(--ds-color-ink)]">
                등록 절차 (편집 채널 = git/PR)
              </span>
            </div>
            <ol className="flex flex-col gap-ds-4 pl-ds-4">
              <li>
                ① 위 YAML을 복사해{" "}
                <code className="rounded-sm bg-[var(--ds-color-surface-muted)] px-ds-4 py-[1px] font-mono text-xs text-[var(--ds-color-ink)]">
                  registry/agents/{form.agentId.trim() || "{agent_id}"}.yaml
                </code>{" "}
                파일로 저장
              </li>
              <li>② git add · commit · PR — 리뷰 후 main 병합 시 실제 등록</li>
              <li>③ 라이브 레지스트리는 서버 재시작 후 적용됩니다.</li>
            </ol>
          </div>
        </div>
      )}
    </div>
  );
}

// ── 폼 프리미티브 ──────────────────────────────────────────────────────────

function Field({
  label,
  required,
  hint,
  children,
}: {
  label: string;
  required?: boolean;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-ds-4">
      <label className="flex items-center gap-ds-4 text-xs font-medium text-[var(--ds-color-ink-muted)]">
        <span className="font-mono">{label}</span>
        {required && (
          <span aria-label="필수" className="text-[var(--ds-color-danger)]">
            *
          </span>
        )}
      </label>
      {children}
      {hint && <p className="text-xs text-[var(--ds-color-ink-subtle)]">{hint}</p>}
    </div>
  );
}

function TextInput({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="h-10 w-full rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)] px-ds-12 text-sm text-[var(--ds-color-ink)] placeholder:text-[var(--ds-color-ink-subtle)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ds-color-primary)]"
    />
  );
}
