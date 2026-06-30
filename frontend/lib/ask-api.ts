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

/* ---- SSE token streaming (POST /ask/stream) — ADR 0031 event contract ---- */

export interface AskMeta {
  answered_by: { owner: string; agent_id: string };
  mode: AnswerMode;
  sources: string[];
}
export interface AskDone {
  mode: AnswerMode;
  sources: string[];
}
export interface AskPending {
  kind: string;
  message: string;
  tracking?: string;
}

export interface AskStreamHandlers {
  // Routed: meta(담당 즉시) → token*(델타 누적) → done(최종 신뢰 배지)
  onMeta?: (m: AskMeta) => void;
  onToken?: (text: string) => void;
  onDone?: (d: AskDone) => void;
  // Pending(다툼/담당 없음/대기)은 비스트림 — 단독 1회
  onPending?: (p: AskPending) => void;
  // 런타임 실패·timeout — 중립 안내만
  onError?: (message: string) => void;
}

// Dispatch a single parsed SSE frame ("event: x\ndata: {json}") to handlers.
// Backend frame shape is fixed by serialize_sse_event (ask_org.py).
function dispatchSseFrame(frame: string, h: AskStreamHandlers): void {
  let name = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (dataLines.length === 0) return;
  let data: unknown;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch {
    return;
  }
  switch (name) {
    case "meta":
      h.onMeta?.(data as AskMeta);
      break;
    case "token":
      h.onToken?.((data as { text: string }).text);
      break;
    case "done":
      h.onDone?.(data as AskDone);
      break;
    case "pending":
      h.onPending?.(data as AskPending);
      break;
    case "error":
      h.onError?.((data as { message: string }).message);
      break;
  }
}

/**
 * POST /api/ask/stream — consume the SSE token stream, dispatching each event.
 * Reads the response body as a stream (the Next proxy pipes text/event-stream
 * through unbuffered) and parses SSE frames split on the blank line.
 */
export async function streamAsk(
  question: string,
  handlers: AskStreamHandlers,
  signal?: AbortSignal
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/ask/stream", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question }),
      signal,
    });
  } catch {
    throw new AskError("네트워크 오류 — 백엔드에 연결할 수 없습니다.");
  }
  if (!res.ok || !res.body) {
    throw new AskError(`스트리밍 요청이 실패했습니다 (HTTP ${res.status}).`, res.status);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      if (frame.trim()) dispatchSseFrame(frame, handlers);
    }
  }
  if (buffer.trim()) dispatchSseFrame(buffer, handlers);
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
