// /console real client. Mirrors web.py:
//   summarize_audit_record (web.py:424) → GET /monitor (list)
//   record (full audit dict)            → GET /monitor/{index}
//   serialize_org_graph    (web.py:440) → GET /org/graph
//   serialize_manager_item (web.py:324) → GET /manager/queue
//
// Operational surface — internal values exposed OK. The session cookie scopes
// /manager/queue to the logged-in manager and gates /monitor + /org/graph on
// being logged in at all (ADR 0016 결정 5: 인증만, 세분 역할 없음).

export interface AuditSummary {
  index: number;
  timestamp: string | null;
  user_id: string | null;
  question: string | null;
  intent: string | null;
  disposition: string | null;
  mode: string | null;
  answered: boolean;
}

export interface OrgNode {
  type: "user" | "card";
  // user
  id?: string;
  manager?: string | null;
  // card
  agent_id?: string;
  owner?: string;
  team?: string;
  domains?: string[];
  maintainer?: string;
}

export interface OrgEdge {
  type: "owns" | "manages" | "maintains";
  source: string;
  target: string;
}

export interface OrgGraph {
  nodes: OrgNode[];
  edges: OrgEdge[];
}

// serialize_manager_item — source is a tagged union (from_unowned/deadlock/dispatch).
export type ManagerSource =
  | { type: "from_unowned"; question: string; escalated_to: string }
  | {
      type: "from_deadlock";
      case_id: string;
      intent: string;
      question: string;
      reason: string;
    }
  | {
      type: "from_dispatch";
      ticket_id: string;
      owner_id: string;
      question: string;
      manager_id: string;
      reason: string;
    };

export interface ManagerItem {
  item_id: string;
  manager_id: string;
  status: string;
  created_at: string;
  source: ManagerSource;
}

export class ConsoleError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "ConsoleError";
    this.status = status;
  }
}

async function getJson<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { accept: "application/json" } });
  } catch {
    throw new ConsoleError("네트워크 오류 — 백엔드에 연결할 수 없습니다.");
  }
  if (res.status === 401) throw new ConsoleError("로그인이 필요합니다.", 401);
  if (!res.ok) throw new ConsoleError(`요청 실패 (HTTP ${res.status}).`, res.status);
  return (await res.json()) as T;
}

/** GET /api/monitor — deduped audit summaries (newest delivery per tracking). */
export function getMonitor(): Promise<AuditSummary[]> {
  return getJson<AuditSummary[]>("/api/monitor");
}

/** GET /api/org/graph — registry projected to {nodes, edges}. */
export function getOrgGraph(): Promise<OrgGraph> {
  return getJson<OrgGraph>("/api/org/graph");
}

/** GET /api/manager/queue — escalation queue for the session manager. */
export function getManagerQueue(): Promise<ManagerItem[]> {
  return getJson<ManagerItem[]>("/api/manager/queue");
}
