"use client";

import { useCallback, useEffect, useState } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import { Tag } from "@/components/ui/tag";

type Policy = { revision_id: string; org_id: string; epoch: number; policy_version: string; policy_digest: string; activated_at: string };
type Axis = Record<string, string | number | boolean | null>;
type OwnerScore = { owner_user_id: string; quality: Axis; supervision: Axis; availability: Axis; freshness: Axis; weak_identity_note: string | null };
type Scorecard = { window: { since: string; until: string }; source_digest: string; owners: OwnerScore[] };
type ReadState<T> = { kind: "idle" | "loading" } | { kind: "ready"; value: T } | { kind: "error"; status: number; message: string };

const HTTP_MESSAGES: Record<number, string> = {
  401: "로그인이 필요하거나 세션이 만료되었습니다.",
  403: "현재 브라우저 세션에는 이 관리자 읽기 권한이 없습니다.",
  404: "관리자 읽기 대상을 찾을 수 없습니다.",
  503: "현재 capability가 unavailable입니다. PolicyRevision/scorecard를 아직 사용할 수 없습니다.",
};
const REFERENCE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const SHA256 = /^[a-f0-9]{64}$/;

export function AdminControlPlanePanel() {
  const [policy, setPolicy] = useState<ReadState<Policy>>({ kind: "idle" });
  const [scorecard, setScorecard] = useState<ReadState<Scorecard>>({ kind: "idle" });

  const readPolicy = useCallback(async () => {
    setPolicy({ kind: "loading" });
    setPolicy(await readOnly("/api/admin/policy", parsePolicy));
  }, []);
  const readScorecard = useCallback(async () => {
    setScorecard({ kind: "loading" });
    setScorecard(await readOnly("/api/admin/scorecard", parseScorecard));
  }, []);

  useEffect(() => {
    void readPolicy();
    void readScorecard();
  }, [readPolicy, readScorecard]);

  return (
    <section className="flex flex-col gap-ds-16 px-ds-16 pb-ds-16 md:px-ds-24" aria-labelledby="admin-control-plane-title">
      <div>
        <h2 id="admin-control-plane-title" className="font-heading text-lg font-semibold">PolicyRevision · 조직 scorecard</h2>
        <p className="mt-ds-4 text-sm text-[var(--ds-color-ink-muted)]">읽기 전용 dedicated BFF만 사용합니다. 정책 문서 원문과 Owner mutation은 이 화면에 노출하지 않습니다.</p>
      </div>
      <div className="grid gap-ds-16 xl:grid-cols-2">
        <ReadCard title="현재 PolicyRevision" state={policy} retry={() => void readPolicy()}><PolicyView value={policy.kind === "ready" ? policy.value : null} /></ReadCard>
        <ReadCard title="조직 scorecard" state={scorecard} retry={() => void readScorecard()}><ScorecardView value={scorecard.kind === "ready" ? scorecard.value : null} /></ReadCard>
      </div>
      <Card><CardBody><StatusBadge tone="neutral" label="Card Owner transfer/revoke · HTTP 503 capability unavailable" /><p className="mt-ds-8 text-sm text-[var(--ds-color-ink-muted)]">v21 same-UoW capability가 연결되기 전까지 전이·해제 요청을 호출하지 않습니다.</p></CardBody></Card>
    </section>
  );
}

function ReadCard<T>({ title, state, retry, children }: { title: string; state: ReadState<T>; retry: () => void; children: React.ReactNode }) {
  return <Card><CardHeader><div className="flex flex-wrap items-center justify-between gap-ds-8"><CardTitle>{title}</CardTitle><button type="button" onClick={retry} disabled={state.kind === "loading"} className="rounded-md border border-[var(--ds-color-border-strong)] px-ds-8 py-[3px] text-xs font-medium disabled:cursor-not-allowed disabled:opacity-60">다시 읽기</button></div></CardHeader><CardBody>{state.kind === "idle" || state.kind === "loading" ? <StatusBadge tone="pending" label={state.kind === "loading" ? "읽는 중…" : "읽기 전"} /> : state.kind === "error" ? <div className="grid gap-ds-8"><StatusBadge tone="danger" label={`HTTP ${state.status} · 읽기 실패`} /><p className="text-sm text-[var(--ds-color-ink-muted)]">{state.message}</p></div> : children}</CardBody></Card>;
}

function PolicyView({ value }: { value: Policy | null }) { return value === null ? null : <dl className="grid gap-ds-8 text-sm"><Row label="Revision ID" value={value.revision_id} /><Row label="Organization" value={value.org_id} /><Row label="Epoch" value={String(value.epoch)} /><Row label="Policy version" value={value.policy_version} /><Row label="Policy digest" value={value.policy_digest} /><Row label="Activated at" value={value.activated_at} /></dl>; }
function ScorecardView({ value }: { value: Scorecard | null }) { return value === null ? null : <div className="grid gap-ds-12 text-sm"><div className="flex flex-wrap gap-ds-8"><Tag tone="info">{value.window.since}</Tag><span aria-hidden>→</span><Tag tone="info">{value.window.until}</Tag></div><p className="break-all font-mono text-xs text-[var(--ds-color-ink-muted)]">source digest: {value.source_digest}</p>{value.owners.length === 0 ? <p className="text-[var(--ds-color-ink-muted)]">해당 기간의 Owner score가 없습니다.</p> : <ul className="grid gap-ds-12">{value.owners.map((owner) => <li key={owner.owner_user_id} className="rounded-md border border-[var(--ds-color-border)] px-ds-12 py-ds-8"><div className="flex flex-wrap items-center gap-ds-8"><code>{owner.owner_user_id}</code><Tag tone="neutral">assignment generation 기준</Tag></div><AxisList label="Quality" axis={owner.quality} /><AxisList label="Supervision" axis={owner.supervision} /><AxisList label="Availability" axis={owner.availability} /><AxisList label="Freshness" axis={owner.freshness} />{owner.weak_identity_note !== null && <p className="mt-ds-8 text-xs text-[var(--ds-color-ink-muted)]">Note: {owner.weak_identity_note}</p>}</li>)}</ul>}</div>; }
function AxisList({ label, axis }: { label: string; axis: Axis }) { return <p className="mt-ds-4 text-xs text-[var(--ds-color-ink-muted)]">{label}: {Object.entries(axis).map(([key, value]) => `${key}=${value === null ? "—" : String(value)}`).join(" · ") || "—"}</p>; }
function Row({ label, value }: { label: string; value: string }) { return <div><dt className="text-xs text-[var(--ds-color-ink-subtle)]">{label}</dt><dd className="mt-ds-4 break-all font-mono text-xs">{value}</dd></div>; }

async function readOnly<T>(path: string, parse: (value: unknown) => T | null): Promise<ReadState<T>> {
  try {
    const response = await fetch(path, { method: "GET", credentials: "include", headers: { Accept: "application/json" }, cache: "no-store" });
    if (!response.ok) return { kind: "error", status: response.status, message: messageFor(response.status) };
    const value: unknown = await response.json().catch(() => null);
    const parsed = parse(value);
    return parsed === null ? { kind: "error", status: 503, message: HTTP_MESSAGES[503] } : { kind: "ready", value: parsed };
  } catch { return { kind: "error", status: 503, message: HTTP_MESSAGES[503] }; }
}

function messageFor(status: number): string { return HTTP_MESSAGES[status] ?? (status === 502 ? HTTP_MESSAGES[503] : "관리자 읽기를 완료하지 못했습니다. 다시 시도해 주세요."); }
function parsePolicy(value: unknown): Policy | null { return object(value) && isReference(value.revision_id) && isReference(value.org_id) && positive(value.epoch) && typeof value.policy_version === "string" && sha(value.policy_digest) && typeof value.activated_at === "string" ? { revision_id: value.revision_id, org_id: value.org_id, epoch: value.epoch, policy_version: value.policy_version, policy_digest: value.policy_digest, activated_at: value.activated_at } : null; }
function parseScorecard(value: unknown): Scorecard | null { if (!object(value) || !object(value.window) || typeof value.window.since !== "string" || typeof value.window.until !== "string" || !sha(value.source_digest) || !Array.isArray(value.owners)) return null; const owners = value.owners.map(parseOwner); return owners.some((owner) => owner === null) ? null : { window: { since: value.window.since, until: value.window.until }, source_digest: value.source_digest, owners: owners as OwnerScore[] }; }
function parseOwner(value: unknown): OwnerScore | null { if (!object(value) || !isReference(value.owner_user_id) || !isAxis(value.quality) || !isAxis(value.supervision) || !isAxis(value.availability) || !isAxis(value.freshness) || (value.weak_identity_note !== null && typeof value.weak_identity_note !== "string")) return null; return { owner_user_id: value.owner_user_id, quality: value.quality, supervision: value.supervision, availability: value.availability, freshness: value.freshness, weak_identity_note: value.weak_identity_note }; }
function isAxis(value: unknown): value is Axis { return object(value) && Object.values(value).every((item) => item === null || typeof item === "string" || typeof item === "number" || typeof item === "boolean"); }
function object(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }
function positive(value: unknown): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value > 0; }
function sha(value: unknown): value is string { return typeof value === "string" && SHA256.test(value); }
function isReference(value: unknown): value is string { return typeof value === "string" && REFERENCE.test(value); }
