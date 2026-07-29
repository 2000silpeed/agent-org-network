"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { PageHeader } from "@/components/app-shell/page-header";
import { LoginGate } from "@/components/session/login-gate";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import {
  getOnboardingStatus,
  createOwnerAuthoringDraft,
  OnboardingError,
  registerAgentCard,
  registerRegistryUser,
  type OnboardingStatus,
} from "@/lib/onboarding-api";

export default function OnboardingPage() {
  return (
    <div className="flex flex-col">
      <PageHeader
        surface="Onboarding"
        persona="운영자"
        title="조직 온보딩"
        description="Registry User 등록부터 Agent Card와 Knowledge OKF 준비까지 이어서 진행합니다."
      />
      <LoginGate surface="조직 온보딩">
        <OnboardingWorkspace />
      </LoginGate>
    </div>
  );
}

function OnboardingWorkspace() {
  const [status, setStatus] = useState<OnboardingStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [form, setForm] = useState({ user_id: "", email: "", manager: "" });
  const [cardForm, setCardForm] = useState({
    agent_id: "", owner: "", team: "", summary: "", domains: "",
  });
  const [knowledgeFiles, setKnowledgeFiles] = useState<File[]>([]);
  const [knowledgeAgentId, setKnowledgeAgentId] = useState("");
  const [knowledgeKey, setKnowledgeKey] = useState(() => crypto.randomUUID());
  const [knowledgeComplete, setKnowledgeComplete] = useState(false);

  const reload = useCallback(async () => {
    setError(null);
    try {
      setStatus(await getOnboardingStatus());
    } catch (cause) {
      setError(
        cause instanceof OnboardingError
          ? cause.status === 503
            ? "중앙 User 등록 capability가 준비되지 않았습니다."
            : cause.message
          : "온보딩 상태를 불러오지 못했습니다.",
      );
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      await registerRegistryUser(
        {
          expected_revision: status?.revision ?? 0,
          user_id: form.user_id.trim(),
          email: form.email.trim(),
          manager: form.manager.trim() || null,
        },
        crypto.randomUUID(),
      );
      setForm({ user_id: "", email: "", manager: "" });
      await reload();
    } catch (cause) {
      setError(
        cause instanceof OnboardingError ? cause.message : "사용자 등록에 실패했습니다.",
      );
    } finally {
      setPending(false);
    }
  }

  async function submitCard(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      await registerAgentCard({
        expected_revision: status?.revision ?? 0,
        agent_id: cardForm.agent_id.trim(),
        owner: cardForm.owner.trim(),
        team: cardForm.team.trim(),
        summary: cardForm.summary.trim(),
        domains: cardForm.domains.split(",").map((value) => value.trim()).filter(Boolean),
        maintainer: null,
        can_answer: [],
        cannot_answer: [],
        approval_when: [],
        collaborate_when: [],
        knowledge_sources: [],
        trust_labels: [],
      }, crypto.randomUUID());
      setCardForm({ agent_id: "", owner: "", team: "", summary: "", domains: "" });
      await reload();
    } catch (cause) {
      setError(cause instanceof OnboardingError ? cause.message : "Agent Card 등록에 실패했습니다.");
    } finally {
      setPending(false);
    }
  }

  async function submitKnowledge(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    setKnowledgeComplete(false);
    try {
      const result = await createOwnerAuthoringDraft(
        knowledgeAgentId,
        knowledgeFiles,
        knowledgeKey,
      );
      if (result.stage === "AwaitingOwnerReview" && result.revision === 1) {
        setKnowledgeComplete(true);
        setKnowledgeKey(crypto.randomUUID());
      }
    } catch (cause) {
      setError(cause instanceof OnboardingError ? cause.message : "OKF 초안 제작에 실패했습니다.");
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="flex flex-col gap-ds-16 px-ds-16 py-ds-16 md:px-ds-24">
      <ol aria-label="온보딩 진행 단계" className="grid gap-ds-8 md:grid-cols-3">
        {(status?.steps ?? [
          { kind: "user", label: "Registry User", state: "current" as const },
          { kind: "card", label: "Agent Card", state: "locked" as const },
          { kind: "knowledge", label: "Knowledge OKF", state: "locked" as const },
        ]).map((step, index) => (
          <li
            key={step.kind}
            className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-16"
          >
            <p className="text-xs text-[var(--ds-color-ink-subtle)]">단계 {index + 1}</p>
            <div className="mt-ds-4 flex items-center justify-between gap-ds-8">
              <strong className="text-sm text-[var(--ds-color-ink)]">{step.label}</strong>
              <StatusBadge
                tone={step.state === "complete" ? "success" : step.state === "current" ? "info" : "neutral"}
                label={step.state === "complete" ? "완료" : step.state === "current" ? "진행 가능" : "잠김"}
              />
            </div>
          </li>
        ))}
      </ol>

      {error && (
        <div role="alert" className="rounded-md border border-[var(--ds-color-danger)] p-ds-12 text-sm">
          {error}
        </div>
      )}

      <section className="grid gap-ds-16 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
        <form onSubmit={submit} className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-16">
          <h2 className="font-heading text-md font-semibold">Registry User 등록</h2>
          <p className="mt-ds-4 text-xs text-[var(--ds-color-ink-subtle)]">
            기존 회사 SSO가 증명할 이메일과 정확히 일치해야 연결됩니다.
          </p>
          {(["user_id", "email", "manager"] as const).map((field) => (
            <label key={field} className="mt-ds-12 block text-xs font-medium">
              {field}
              <input
                required={field !== "manager"}
                type={field === "email" ? "email" : "text"}
                value={form[field]}
                onChange={(event) => setForm((current) => ({ ...current, [field]: event.target.value }))}
                className="mt-ds-4 h-10 w-full rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)] px-ds-12 text-sm"
              />
            </label>
          ))}
          <Button className="mt-ds-16" type="submit" disabled={pending}>
            {pending ? "등록 중…" : "사용자 등록"}
          </Button>
        </form>

        <div className="overflow-hidden rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)]">
          <div className="border-b border-[var(--ds-color-border)] p-ds-16">
            <h2 className="font-heading text-md font-semibold">등록된 사용자</h2>
          </div>
          <ul className="divide-y divide-[var(--ds-color-border)]">
            {status?.users.map((user) => (
              <li key={user.user_id} className="flex items-center justify-between gap-ds-12 p-ds-16">
                <div className="min-w-0">
                  <p className="font-mono text-sm">{user.user_id}</p>
                  <p className="truncate text-xs text-[var(--ds-color-ink-subtle)]">{user.email}</p>
                </div>
                <StatusBadge
                  tone={user.sso_link_status === "verified_email_match" ? "success" : "neutral"}
                  label={user.sso_link_status === "verified_email_match" ? "SSO 연결됨" : "미확인"}
                />
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section className="grid gap-ds-16 lg:grid-cols-2">
        <form onSubmit={submitCard} className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-16">
          <h2 className="font-heading text-md font-semibold">Agent Card 라이브 등록</h2>
          <p className="mt-ds-4 text-xs text-[var(--ds-color-ink-subtle)]">
            {status?.cardCapability === "unavailable"
              ? "중앙 Agent Card capability가 준비되지 않았습니다."
              : "중앙 Registry에 즉시 등록됩니다. 미리보기나 YAML 저장만으로 완료 처리되지 않습니다."}
          </p>
          {(["agent_id", "owner", "team", "summary", "domains"] as const).map((field) => (
            <label key={field} className="mt-ds-12 block text-xs font-medium">
              {field}{field === "domains" ? " (쉼표 구분)" : ""}
              <input
                required
                value={cardForm[field]}
                onChange={(event) => setCardForm((current) => ({ ...current, [field]: event.target.value }))}
                className="mt-ds-4 h-10 w-full rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)] px-ds-12 text-sm"
              />
            </label>
          ))}
          <Button className="mt-ds-16" type="submit" disabled={pending || status?.steps[1]?.state === "locked" || status?.cardCapability === "unavailable"}>
            {pending ? "등록 중…" : "Agent Card 등록"}
          </Button>
        </form>
        <div className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-16">
          <h2 className="font-heading text-md font-semibold">내 Agent Card</h2>
          <ul className="mt-ds-8 divide-y divide-[var(--ds-color-border)]">
            {status?.cards.map((card) => (
              <li key={card.agent_id} className="py-ds-12">
                <p className="font-mono text-sm">{card.agent_id}</p>
                <p className="text-xs text-[var(--ds-color-ink-subtle)]">{card.summary}</p>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section className="rounded-lg border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-16">
        <h2 className="font-heading text-md font-semibold">문서 기반 Knowledge OKF 초안</h2>
        <p className="mt-ds-4 text-xs text-[var(--ds-color-ink-subtle)]">
          원문과 전체 초안은 Card Owner 설치에 암호화 저장되며, 중앙에는 digest와 개수만 전송됩니다.
        </p>
        <form onSubmit={submitKnowledge} className="mt-ds-12 grid gap-ds-12 md:grid-cols-2">
          <label className="text-xs font-medium">
            Agent Card
            <select
              required
              value={knowledgeAgentId}
              onChange={(event) => setKnowledgeAgentId(event.target.value)}
              className="mt-ds-4 h-10 w-full rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-canvas)] px-ds-12 text-sm"
            >
              <option value="">선택</option>
              {status?.cards.map((card) => <option key={card.agent_id} value={card.agent_id}>{card.agent_id}</option>)}
            </select>
          </label>
          <label className="text-xs font-medium">
            원문 문서
            <input
              required
              multiple
              type="file"
              accept=".md,.txt,text/markdown,text/plain"
              onChange={(event) => {
                setKnowledgeFiles(Array.from(event.target.files ?? []));
                setKnowledgeComplete(false);
              }}
              className="mt-ds-4 block w-full text-sm"
            />
          </label>
          <div className="md:col-span-2 flex items-center gap-ds-12">
            <Button
              type="submit"
              disabled={pending || knowledgeFiles.length === 0 || status?.steps[2]?.state === "locked"}
            >
              {pending ? "초안 제작 중…" : "OKF 초안 제작"}
            </Button>
            {knowledgeComplete && <StatusBadge tone="success" label="Owner 검토 대기" />}
          </div>
        </form>
      </section>
    </main>
  );
}
