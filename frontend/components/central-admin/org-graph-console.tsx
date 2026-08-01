"use client";

import { useCallback, useEffect, useState } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import { Tag } from "@/components/ui/tag";

type UserNode = {
  kind: "user";
  user_id: string;
  manager_user_id: string | null;
};

type AgentCardNode = {
  kind: "agent_card";
  card_id: string;
  card_revision: number;
  team: string;
  assignment_status: "active" | "revoked" | "none";
  assignment_generation: number | null;
  current_owner_user_id: string | null;
  recorded_owner_user_id: string;
};

type GraphNode = UserNode | AgentCardNode;
type GraphEdge = { kind: "owns" | "manages" | "maintains"; source_id: string; target_id: string };
type OrgGraph = {
  registry_revision: number;
  policy_epoch: number;
  policy_digest: string;
  source_digest: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
};

type State =
  | { kind: "loading" }
  | { kind: "ready"; graph: OrgGraph }
  | { kind: "error"; status: number; message: string };

const HTTP_MESSAGES: Record<number, string> = {
  401: "로그인이 필요하거나 세션이 만료되었습니다.",
  403: "현재 브라우저 세션에는 조직 그래프 읽기 권한이 없습니다.",
  404: "조직 그래프를 찾을 수 없습니다.",
  503: "조직 그래프 capability가 현재 unavailable입니다. 잠시 뒤 다시 시도해 주세요.",
};
const SHA256 = /^[a-f0-9]{64}$/;
const REFERENCE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

export function OrgGraphConsole() {
  const [state, setState] = useState<State>({ kind: "loading" });

  const refresh = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const response = await fetch("/api/console/org", {
        method: "GET",
        credentials: "include",
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) {
        setState({ kind: "error", status: response.status, message: messageFor(response.status) });
        return;
      }
      const value: unknown = await response.json().catch(() => null);
      const graph = parseGraph(value);
      if (graph === null) {
        setState({ kind: "error", status: 503, message: HTTP_MESSAGES[503] });
        return;
      }
      setState({ kind: "ready", graph });
    } catch {
      setState({ kind: "error", status: 503, message: HTTP_MESSAGES[503] });
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <section className="flex flex-col gap-ds-16 px-ds-16 py-ds-16 md:px-ds-24" aria-labelledby="org-graph-title">
      <div className="flex flex-wrap items-center justify-between gap-ds-8">
        <div>
          <h2 id="org-graph-title" className="font-heading text-lg font-semibold">조직 그래프</h2>
          <p className="mt-ds-4 text-sm text-[var(--ds-color-ink-muted)]">
            현재 Registry와 assignment의 안전한 User·Agent Card 요약만 표시합니다. Policy 문서는 이 화면에서 읽지 않습니다.
          </p>
        </div>
        <button type="button" onClick={() => void refresh()} disabled={state.kind === "loading"} className="rounded-md border border-[var(--ds-color-border-strong)] px-ds-12 py-ds-8 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-60">
          {state.kind === "loading" ? "읽는 중…" : "새로 고침"}
        </button>
      </div>

      {state.kind === "loading" && <StatusBadge tone="pending" label="조직 그래프를 읽는 중입니다." />}
      {state.kind === "error" && <GraphError status={state.status} message={state.message} onRetry={() => void refresh()} />}
      {state.kind === "ready" && <GraphView graph={state.graph} />}
    </section>
  );
}

function GraphError({ status, message, onRetry }: { status: number; message: string; onRetry: () => void }) {
  return (
    <Card>
      <CardBody className="flex flex-wrap items-center justify-between gap-ds-12">
        <div className="grid gap-ds-8">
          <StatusBadge tone="danger" label={`HTTP ${status} · 조직 그래프 읽기 실패`} />
          <p className="text-sm text-[var(--ds-color-ink-muted)]">{message}</p>
        </div>
        <button type="button" onClick={onRetry} className="rounded-md border border-[var(--ds-color-border-strong)] px-ds-12 py-ds-8 text-sm font-medium">다시 시도</button>
      </CardBody>
    </Card>
  );
}

function GraphView({ graph }: { graph: OrgGraph }) {
  const users = graph.nodes.filter((node): node is UserNode => node.kind === "user");
  const cards = graph.nodes.filter((node): node is AgentCardNode => node.kind === "agent_card");
  return (
    <div className="grid gap-ds-16 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <Card>
        <CardHeader><CardTitle>Snapshot</CardTitle></CardHeader>
        <CardBody className="grid gap-ds-12 sm:grid-cols-2">
          <Metric label="Registry revision" value={String(graph.registry_revision)} />
          <Metric label="Policy epoch" value={String(graph.policy_epoch)} />
          <DigestMetric label="Policy digest" value={graph.policy_digest} />
          <DigestMetric label="Source digest" value={graph.source_digest} />
        </CardBody>
      </Card>

      <Card>
        <CardHeader><CardTitle>Edges ({graph.edges.length})</CardTitle></CardHeader>
        <CardBody>
          {graph.edges.length === 0 ? <p className="text-sm text-[var(--ds-color-ink-muted)]">활성 관계가 없습니다.</p> : <ul className="grid gap-ds-8 text-sm">{graph.edges.map((edge) => <li key={`${edge.kind}:${edge.source_id}:${edge.target_id}`} className="flex flex-wrap items-center gap-ds-8"><Tag tone="neutral">{edge.kind}</Tag><code>{edge.source_id}</code><span aria-hidden>→</span><code>{edge.target_id}</code></li>)}</ul>}
        </CardBody>
      </Card>

      <Card>
        <CardHeader><CardTitle>User ({users.length})</CardTitle></CardHeader>
        <CardBody>{users.length === 0 ? <p className="text-sm text-[var(--ds-color-ink-muted)]">User가 없습니다.</p> : <ul className="grid gap-ds-12">{users.map((user) => <li key={user.user_id} className="rounded-md border border-[var(--ds-color-border)] px-ds-12 py-ds-8"><div className="flex flex-wrap items-center gap-ds-8"><code>{user.user_id}</code><Tag tone="info">User</Tag></div><p className="mt-ds-4 text-xs text-[var(--ds-color-ink-muted)]">Manager: {user.manager_user_id ?? "root"}</p></li>)}</ul>}</CardBody>
      </Card>

      <Card>
        <CardHeader><CardTitle>Agent Card ({cards.length})</CardTitle></CardHeader>
        <CardBody>{cards.length === 0 ? <p className="text-sm text-[var(--ds-color-ink-muted)]">Agent Card가 없습니다.</p> : <ul className="grid gap-ds-12">{cards.map((card) => <li key={card.card_id} className="rounded-md border border-[var(--ds-color-border)] px-ds-12 py-ds-8"><div className="flex flex-wrap items-center gap-ds-8"><code>{card.card_id}</code><Tag tone={card.assignment_status === "active" ? "success" : "neutral"}>{card.assignment_status}</Tag><span className="text-sm text-[var(--ds-color-ink-muted)]">{card.team}</span></div><p className="mt-ds-4 text-xs text-[var(--ds-color-ink-muted)]">Current Owner: {card.current_owner_user_id ?? "없음"} · Recorded Owner: {card.recorded_owner_user_id} · Card revision: {card.card_revision}</p></li>)}</ul>}</CardBody>
      </Card>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) { return <div><dt className="text-xs text-[var(--ds-color-ink-subtle)]">{label}</dt><dd className="mt-ds-4 font-mono text-sm">{value}</dd></div>; }
function DigestMetric({ label, value }: { label: string; value: string }) { return <div><dt className="text-xs text-[var(--ds-color-ink-subtle)]">{label}</dt><dd className="mt-ds-4 break-all font-mono text-xs">{value}</dd></div>; }

function messageFor(status: number): string {
  if (status === 502) return HTTP_MESSAGES[503];
  return HTTP_MESSAGES[status] ?? "조직 그래프를 읽지 못했습니다. 응답을 확인한 뒤 다시 시도해 주세요.";
}

function parseGraph(value: unknown): OrgGraph | null {
  if (!object(value) || !positive(value.registry_revision) || !positive(value.policy_epoch) || !sha(value.policy_digest) || !sha(value.source_digest) || !Array.isArray(value.nodes) || !value.nodes.every((node) => parseNode(node) !== null) || !Array.isArray(value.edges) || !value.edges.every((edge) => parseEdge(edge) !== null)) return null;
  return { registry_revision: value.registry_revision, policy_epoch: value.policy_epoch, policy_digest: value.policy_digest, source_digest: value.source_digest, nodes: value.nodes.map((node) => parseNode(node) as GraphNode), edges: value.edges.map((edge) => parseEdge(edge) as GraphEdge) };
}

function parseNode(value: unknown): GraphNode | null {
  if (!object(value) || typeof value.kind !== "string") return null;
  if (value.kind === "user") return isReference(value.user_id) && (value.manager_user_id === null || isReference(value.manager_user_id)) ? { kind: "user", user_id: value.user_id, manager_user_id: value.manager_user_id } : null;
  return value.kind === "agent_card" && isReference(value.card_id) && positive(value.card_revision) && typeof value.team === "string" && ["active", "revoked", "none"].includes(String(value.assignment_status)) && (value.assignment_generation === null || positive(value.assignment_generation)) && (value.current_owner_user_id === null || isReference(value.current_owner_user_id)) && isReference(value.recorded_owner_user_id) ? { kind: "agent_card", card_id: value.card_id, card_revision: value.card_revision, team: value.team, assignment_status: value.assignment_status as AgentCardNode["assignment_status"], assignment_generation: value.assignment_generation, current_owner_user_id: value.current_owner_user_id, recorded_owner_user_id: value.recorded_owner_user_id } : null;
}

function parseEdge(value: unknown): GraphEdge | null { return object(value) && ["owns", "manages", "maintains"].includes(String(value.kind)) && isReference(value.source_id) && isReference(value.target_id) ? { kind: value.kind as GraphEdge["kind"], source_id: value.source_id, target_id: value.target_id } : null; }
function object(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }
function positive(value: unknown): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value > 0; }
function sha(value: unknown): value is string { return typeof value === "string" && SHA256.test(value); }
function isReference(value: unknown): value is string { return typeof value === "string" && REFERENCE.test(value); }
