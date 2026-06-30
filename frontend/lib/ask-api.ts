// Real /ask backend client. Mirrors `serialize_reply` (web.py:203) exactly —
// only the fields the endpoint exposes are typed here. Routing internals
// (scores, candidate math) are stripped server-side and MUST NOT appear.
//
// Requests go through the Next proxy (/api/* → FastAPI), so the anonymous
// session cookie (aon_uid) rides along same-origin.

export type AnswerMode = "full" | "draft_only" | "backup";

export interface AnsweredReply {
  type: "answered";
  text: string;
  answered_by: { owner: string; agent_id: string };
  mode: AnswerMode;
  sources: string[];
}

export interface PendingReply {
  type: "pending";
  kind: string;
  message: string;
  tracking?: string;
}

export type OrgReply = AnsweredReply | PendingReply;

export class AskError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "AskError";
    this.status = status;
  }
}

function isOrgReply(value: unknown): value is OrgReply {
  if (typeof value !== "object" || value === null) return false;
  const t = (value as { type?: unknown }).type;
  return t === "answered" || t === "pending";
}

/** POST /api/ask — send a question, get the first reply (Answered or Pending). */
export async function postAsk(question: string): Promise<OrgReply> {
  let res: Response;
  try {
    res = await fetch("/api/ask", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question }),
    });
  } catch {
    throw new AskError("네트워크 오류 — 백엔드에 연결할 수 없습니다.");
  }
  if (!res.ok) {
    throw new AskError(`요청이 실패했습니다 (HTTP ${res.status}).`, res.status);
  }
  const data: unknown = await res.json();
  if (!isOrgReply(data)) {
    throw new AskError("예상치 못한 응답 형식입니다.");
  }
  return data;
}

/**
 * GET /api/ask/{tracking} — retrieve a deferred reply. Returns null on 404
 * (not ready yet / unknown token), so the caller can keep polling.
 */
export async function getAsk(tracking: string): Promise<OrgReply | null> {
  let res: Response;
  try {
    res = await fetch(`/api/ask/${encodeURIComponent(tracking)}`, {
      method: "GET",
    });
  } catch {
    throw new AskError("네트워크 오류 — 답을 회수하지 못했습니다.");
  }
  if (res.status === 404) return null;
  if (!res.ok) {
    throw new AskError(`답 회수 실패 (HTTP ${res.status}).`, res.status);
  }
  const data: unknown = await res.json();
  if (!isOrgReply(data)) {
    throw new AskError("예상치 못한 응답 형식입니다.");
  }
  return data;
}

const POLL_INTERVAL_MS = 700;
const POLL_MAX_ATTEMPTS = 30; // ~21s cap

/**
 * Poll GET /api/ask/{tracking} until an Answered (or terminal Pending) reply
 * arrives, or the attempt cap is hit. A Pending reply with the SAME tracking
 * means "still working" → keep polling.
 */
export async function pollAsk(tracking: string): Promise<OrgReply> {
  for (let attempt = 0; attempt < POLL_MAX_ATTEMPTS; attempt++) {
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    const reply = await getAsk(tracking);
    if (reply === null) continue;
    if (reply.type === "answered") return reply;
    // Pending again but no further tracking to chase → terminal, surface it.
    if (reply.tracking === undefined || reply.tracking !== tracking) return reply;
  }
  throw new AskError("답변이 지연되고 있습니다. 잠시 후 다시 시도해 주세요.");
}

/* ---- UI mapping helpers (kept here so the contract lives in one place) ---- */

import type { StatusTone } from "@/components/ui/status-badge";

/** mode → confidence chip (label + tone). */
export const modeMeta: Record<
  AnswerMode,
  { label: string; tone: StatusTone }
> = {
  full: { label: "승인 완료", tone: "success" },
  draft_only: { label: "초안 대기", tone: "warning" },
  backup: { label: "백업 답변", tone: "info" },
};

/** pending kind → trace label shown in the staged routing trace. */
export function pendingTraceLabel(kind: string): string {
  switch (kind) {
    case "unowned":
      return "담당 없음 — 매니저에게 에스컬레이션";
    case "dispatched":
      return "담당에게 전달됨 — 답변 대기";
    case "contested":
      return "후보 검토 중 — 담당 지정 대기";
    default:
      return "처리 중";
  }
}
