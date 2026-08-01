"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { StepProgress } from "@/components/ui/step-progress";
import { type AgentCardCommand, type AgentCardRow, type OnboardingStatus, type RegistryUserRow, listAgentCards, listRegistryUsers, getOnboardingStatus, OnboardingError, parseListInput, registerAgentCard, registerRegistryUser } from "@/lib/onboarding-api";

type AdmissionData = { status: OnboardingStatus; users: RegistryUserRow[]; cards: AgentCardRow[] };
const emptyCard = { agent_id: "", owner: "", team: "", summary: "", domains: "", maintainer: "", can_answer: "", cannot_answer: "", approval_when: "", collaborate_when: "", knowledge_sources: "", trust_labels: "" };

export function AdmissionConsole({ guided }: { guided: boolean }) {
  const [data, setData] = useState<AdmissionData | null>(null);
  const [message, setMessage] = useState("Central SSO 세션을 확인하는 중입니다.");
  const [pending, setPending] = useState<"user" | "card" | null>(null);
  const requestKeys = useRef(new Map<string, string>());
  const refresh = useCallback(async () => {
    try {
      const [status, users, cards] = await Promise.all([getOnboardingStatus(), listRegistryUsers(), listAgentCards()]);
      setData({ status, users, cards }); setMessage("");
    } catch (error) { setData(null); setMessage(error instanceof OnboardingError ? error.message : "Central 등록 상태를 읽지 못했습니다."); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  async function submitUser(command: { user_id: string; email: string; manager: string }) {
    if (!data) return; setPending("user"); setMessage("");
    const body = { expected_revision: data.status.revision, user_id: command.user_id.trim(), email: command.email.trim(), manager: command.manager.trim() || null };
    const fingerprint = JSON.stringify(body);
    try { const result = await registerRegistryUser(body, idempotencyKey(requestKeys.current, fingerprint)); requestKeys.current.delete(fingerprint); setMessage(result.replayed ? "같은 User 등록 요청을 다시 확인했습니다." : "Registry User를 등록했습니다."); await refresh(); }
    catch (error) { setMessage(error instanceof OnboardingError ? error.message : "User 등록을 완료하지 못했습니다."); } finally { setPending(null); }
  }
  async function submitCard(command: typeof emptyCard) {
    if (!data) return; setPending("card"); setMessage("");
    const body: AgentCardCommand = { expected_revision: data.status.revision, agent_id: command.agent_id.trim(), owner: command.owner.trim(), team: command.team.trim(), summary: command.summary.trim(), domains: parseListInput(command.domains), maintainer: command.maintainer.trim() || null, can_answer: parseListInput(command.can_answer), cannot_answer: parseListInput(command.cannot_answer), approval_when: parseListInput(command.approval_when), collaborate_when: parseListInput(command.collaborate_when), knowledge_sources: parseListInput(command.knowledge_sources), trust_labels: parseListInput(command.trust_labels) };
    const fingerprint = JSON.stringify(body);
    try { const result = await registerAgentCard(body, idempotencyKey(requestKeys.current, fingerprint)); requestKeys.current.delete(fingerprint); setMessage(result.replayed ? "같은 Agent Card 등록 요청을 다시 확인했습니다." : "Agent Card를 등록했습니다."); await refresh(); }
    catch (error) { setMessage(error instanceof OnboardingError ? error.message : "Agent Card를 등록하지 못했습니다."); } finally { setPending(null); }
  }
  const revision = data?.status.revision ?? 0;
  return <div className="flex flex-col gap-ds-16 px-ds-16 py-ds-16 md:px-ds-24">
    <p role="status" aria-live="polite" className="min-h-5 text-sm text-[var(--ds-color-ink-muted)]">{message}</p>
    {guided && data && <Card><CardBody><StepProgress steps={data.status.steps.map((step) => ({ id: step.kind, label: step.label, state: step.state === "complete" ? "done" : step.state === "current" ? "active" : "pending" }))} /></CardBody></Card>}
    {data && <p className="text-sm text-[var(--ds-color-ink-muted)]">공유 Registry revision: <strong>{revision}</strong>. 등록 성공 뒤 목록과 revision을 새로 읽습니다.</p>}
    <div className="grid gap-ds-16 xl:grid-cols-2">
      <UserRegistration users={data?.users ?? []} disabled={!data} pending={pending === "user"} onSubmit={submitUser} />
      <CardRegistration users={data?.users ?? []} cards={data?.cards ?? []} disabled={!data} pending={pending === "card"} onSubmit={submitCard} />
    </div>
    {guided && <Card id="card-owner-installation"><CardHeader><CardTitle>Card Owner Installation</CardTitle></CardHeader><CardBody><p className="text-sm text-[var(--ds-color-ink-muted)]">소유 Agent Card가 준비되면 Card Owner 설치 artifact로 이어집니다. 이 Central 화면은 local install, pairing, source upload를 수행하거나 credential을 보관하지 않습니다.</p></CardBody></Card>}
    {!guided && <Card><CardBody><p className="text-sm text-[var(--ds-color-ink-muted)]">이 화면은 register-only입니다. ownership transfer/revoke와 scorecard는 다음 운영 단계에서 제공합니다. Card Owner local install은 온보딩 handoff에서 진행합니다.</p></CardBody></Card>}
  </div>;
}

function UserRegistration({ users, disabled, pending, onSubmit }: { users: RegistryUserRow[]; disabled: boolean; pending: boolean; onSubmit: (value: { user_id: string; email: string; manager: string }) => Promise<void> }) {
  const [form, setForm] = useState({ user_id: "", email: "", manager: "" });
  return <Card><CardHeader><CardTitle>Registry User 등록</CardTitle></CardHeader><CardBody><form className="grid gap-ds-12" onSubmit={(event) => { event.preventDefault(); void onSubmit(form); }}>
    <Field label="User ID" value={form.user_id} onChange={(user_id) => setForm({ ...form, user_id })} required />
    <Field label="이메일" type="email" value={form.email} onChange={(email) => setForm({ ...form, email })} required />
    <label className="grid gap-ds-4 text-sm font-medium">Manager (선택)<select aria-label="Manager" value={form.manager} onChange={(event) => setForm({ ...form, manager: event.target.value })} className="rounded-md border border-[var(--ds-color-border-strong)] bg-[var(--ds-color-surface)] px-ds-12 py-ds-8"><option value="">관리자 없음 (root)</option>{users.map((user) => <option key={user.user_id} value={user.user_id}>{user.user_id}</option>)}</select></label>
    <Button type="submit" loading={pending} disabled={disabled}>Registry User 등록</Button>
  </form><RowList title="현재 Registry User" rows={users.map((user) => `${user.user_id}${user.email ? ` · ${user.email}` : ""}`)} /></CardBody></Card>;
}

function CardRegistration({ users, cards, disabled, pending, onSubmit }: { users: RegistryUserRow[]; cards: AgentCardRow[]; disabled: boolean; pending: boolean; onSubmit: (value: typeof emptyCard) => Promise<void> }) {
  const [form, setForm] = useState(emptyCard);
  return <Card><CardHeader><CardTitle>Agent Card 등록</CardTitle></CardHeader><CardBody><form className="grid gap-ds-12" onSubmit={(event) => { event.preventDefault(); void onSubmit(form); }}>
    <Field label="Agent Card ID" value={form.agent_id} onChange={(agent_id) => setForm({ ...form, agent_id })} required />
    <label className="grid gap-ds-4 text-sm font-medium">Card Owner<select aria-label="Card Owner" required value={form.owner} onChange={(event) => setForm({ ...form, owner: event.target.value })} className="rounded-md border border-[var(--ds-color-border-strong)] bg-[var(--ds-color-surface)] px-ds-12 py-ds-8"><option value="">선택</option>{users.map((user) => <option key={user.user_id} value={user.user_id}>{user.user_id}</option>)}</select></label>
    <Field label="Team" value={form.team} onChange={(team) => setForm({ ...form, team })} required /><Field label="Summary" value={form.summary} onChange={(summary) => setForm({ ...form, summary })} required />
    <ListField label="Domains (쉼표 또는 줄바꿈)" value={form.domains} onChange={(domains) => setForm({ ...form, domains })} required /><label className="grid gap-ds-4 text-sm font-medium">Maintainer (선택)<select aria-label="Maintainer" value={form.maintainer} onChange={(event) => setForm({ ...form, maintainer: event.target.value })} className="rounded-md border border-[var(--ds-color-border-strong)] bg-[var(--ds-color-surface)] px-ds-12 py-ds-8"><option value="">없음</option>{users.map((user) => <option key={user.user_id} value={user.user_id}>{user.user_id}</option>)}</select></label>
    <ListField label="Can answer" value={form.can_answer} onChange={(can_answer) => setForm({ ...form, can_answer })} /><ListField label="Cannot answer" value={form.cannot_answer} onChange={(cannot_answer) => setForm({ ...form, cannot_answer })} /><ListField label="Approval when" value={form.approval_when} onChange={(approval_when) => setForm({ ...form, approval_when })} /><ListField label="Collaborate when" value={form.collaborate_when} onChange={(collaborate_when) => setForm({ ...form, collaborate_when })} /><ListField label="Public reference paths (본문 업로드 아님)" value={form.knowledge_sources} onChange={(knowledge_sources) => setForm({ ...form, knowledge_sources })} /><ListField label="Trust labels" value={form.trust_labels} onChange={(trust_labels) => setForm({ ...form, trust_labels })} />
    <Button type="submit" loading={pending} disabled={disabled}>Agent Card 등록</Button>
  </form><RowList title="현재 Agent Card" rows={cards.map((card) => `${card.agent_id} · ${card.owner} · ${card.team}`)} /></CardBody></Card>;
}
function Field({ label, value, onChange, required, type = "text" }: { label: string; value: string; onChange: (value: string) => void; required?: boolean; type?: string }) { return <label className="grid gap-ds-4 text-sm font-medium">{label}<input type={type} required={required} value={value} onChange={(event) => onChange(event.target.value)} className="rounded-md border border-[var(--ds-color-border-strong)] bg-[var(--ds-color-surface)] px-ds-12 py-ds-8" /></label>; }
function ListField({ label, value, onChange, required }: { label: string; value: string; onChange: (value: string) => void; required?: boolean }) { return <label className="grid gap-ds-4 text-sm font-medium">{label}<textarea required={required} value={value} onChange={(event) => onChange(event.target.value)} rows={2} className="rounded-md border border-[var(--ds-color-border-strong)] bg-[var(--ds-color-surface)] px-ds-12 py-ds-8" /></label>; }
function RowList({ title, rows }: { title: string; rows: string[] }) { return <section className="mt-ds-16"><h4 className="text-sm font-semibold">{title}</h4><ul className="mt-ds-8 list-disc pl-ds-24 text-sm text-[var(--ds-color-ink-muted)]">{rows.length ? rows.map((row) => <li key={row}>{row}</li>) : <li>아직 없습니다.</li>}</ul></section>; }
function idempotencyKey(keys: Map<string, string>, fingerprint: string): string {
  const previous = keys.get(fingerprint); if (previous !== undefined) return previous;
  const value = globalThis.crypto?.randomUUID?.().replace(/-/g, "_");
  if (value === undefined) throw new OnboardingError("안전한 요청 키를 만들 수 없습니다.", 503);
  keys.set(fingerprint, value); return value;
}
