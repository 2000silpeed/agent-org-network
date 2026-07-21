// P17 Request-first /ask client. request_id is the only request identity.
// Legacy `tracking` is validated as an identity alias and discarded; it is
// never treated as a bearer token or a second lifecycle key.

import type { StatusTone } from "@/components/ui/status-badge";

export type AnswerMode = "full" | "draft_only" | "backup";
export type ReviewStatus = "not_required" | "approved";
export type RequestState =
  | "received"
  | "ready_to_dispatch"
  | "awaiting_answer"
  | "awaiting_conflict"
  | "awaiting_manager"
  | "awaiting_approval"
  | "answered"
  | "declined"
  | "failed";
export type PendingKind = "routing" | "routed" | "contested" | "unowned";
export type LegacyPendingKind = "dispatched" | "contested" | "unowned";

interface RequestReply {
  request_id: string;
}

export interface AnsweredReply extends RequestReply {
  type: "answered";
  record_id: string;
  text: string;
  answered_by: { owner: string; agent_id: string };
  mode: AnswerMode;
  sources: string[];
  review_status: ReviewStatus;
}

export interface PendingReply extends RequestReply {
  type: "pending";
  kind: LegacyPendingKind;
  state: Exclude<RequestState, "answered" | "declined" | "failed">;
  retryable: boolean;
  message: string;
}

export interface DeclinedReply extends RequestReply {
  type: "declined";
  reason_code: string;
  message: string;
}

export interface FailedReply extends RequestReply {
  type: "failed";
  error_code: string;
  message: string;
}

export type OrgReply = AnsweredReply | PendingReply | DeclinedReply | FailedReply;

export interface RequestPendingResult extends RequestReply {
  type: "pending";
  kind: PendingKind;
  state: Exclude<RequestState, "answered" | "declined" | "failed">;
  retryable: boolean;
  message: string;
}

export type RequestResult =
  | AnsweredReply
  | RequestPendingResult
  | DeclinedReply
  | FailedReply;

export class AskError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "AskError";
    this.status = status;
  }
}

type JsonObject = Record<string, unknown>;

const FORBIDDEN_INTERNAL_KEYS = [
  "route",
  "routes",
  "candidate",
  "candidates",
  "policy",
  "policy_version",
  "confidence",
  "reason",
  "manager_id",
  "ticket_id",
] as const;

const ANSWER_MODES = new Set<AnswerMode>(["full", "draft_only", "backup"]);
const REVIEW_STATUSES = new Set<ReviewStatus>(["not_required", "approved"]);
const REQUEST_STATES = new Set<RequestState>([
  "received",
  "ready_to_dispatch",
  "awaiting_answer",
  "awaiting_conflict",
  "awaiting_manager",
  "awaiting_approval",
  "answered",
  "declined",
  "failed",
]);
const LEGACY_PENDING_KINDS = new Set<LegacyPendingKind>([
  "dispatched",
  "contested",
  "unowned",
]);
const STREAM_PENDING_KINDS = new Set<PendingKind>([
  "routing",
  "routed",
  "contested",
  "unowned",
]);

function asObject(value: unknown): JsonObject | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  return value as JsonObject;
}

function nonBlank(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function hasInternalFields(value: JsonObject): boolean {
  return FORBIDDEN_INTERNAL_KEYS.some((key) => key in value);
}

function stringArray(value: unknown): string[] | null {
  if (!Array.isArray(value) || !value.every(nonBlank)) return null;
  return [...value];
}

function answerMode(value: unknown): AnswerMode | null {
  return typeof value === "string" && ANSWER_MODES.has(value as AnswerMode)
    ? (value as AnswerMode)
    : null;
}

function reviewStatus(value: unknown): ReviewStatus | null {
  return typeof value === "string" && REVIEW_STATUSES.has(value as ReviewStatus)
    ? (value as ReviewStatus)
    : null;
}

function requestState(value: unknown): RequestState | null {
  return typeof value === "string" && REQUEST_STATES.has(value as RequestState)
    ? (value as RequestState)
    : null;
}

/** Strictly parse the user-facing legacy JSON envelope. */
export function parseOrgReply(value: unknown): OrgReply | null {
  const raw = asObject(value);
  if (!raw || hasInternalFields(raw) || !nonBlank(raw.request_id) || !nonBlank(raw.type)) {
    return null;
  }

  if (raw.type === "answered") {
    const attribution = asObject(raw.answered_by);
    const mode = answerMode(raw.mode);
    const sources = stringArray(raw.sources);
    const review = reviewStatus(raw.review_status);
    if (
      !attribution ||
      hasInternalFields(attribution) ||
      !nonBlank(raw.record_id) ||
      !nonBlank(raw.text) ||
      !nonBlank(attribution.owner) ||
      !nonBlank(attribution.agent_id) ||
      !mode ||
      !sources ||
      !review
    ) {
      return null;
    }
    return {
      type: "answered",
      request_id: raw.request_id,
      record_id: raw.record_id,
      text: raw.text,
      answered_by: {
        owner: attribution.owner,
        agent_id: attribution.agent_id,
      },
      mode,
      sources,
      review_status: review,
    };
  }

  if (raw.type === "pending") {
    const state = requestState(raw.state);
    const kind = raw.kind;
    if (
      typeof kind !== "string" ||
      !LEGACY_PENDING_KINDS.has(kind as LegacyPendingKind) ||
      !state ||
      state === "answered" ||
      state === "declined" ||
      state === "failed" ||
      typeof raw.retryable !== "boolean" ||
      !nonBlank(raw.message) ||
      raw.tracking !== raw.request_id
    ) {
      return null;
    }
    return {
      type: "pending",
      request_id: raw.request_id,
      kind: kind as LegacyPendingKind,
      state,
      retryable: raw.retryable,
      message: raw.message,
    };
  }

  if (raw.type === "declined" && nonBlank(raw.reason_code) && nonBlank(raw.message)) {
    return {
      type: "declined",
      request_id: raw.request_id,
      reason_code: raw.reason_code,
      message: raw.message,
    };
  }

  if (raw.type === "failed" && nonBlank(raw.error_code) && nonBlank(raw.message)) {
    return {
      type: "failed",
      request_id: raw.request_id,
      error_code: raw.error_code,
      message: raw.message,
    };
  }
  return null;
}

/** Parse the native /requests/{request_id} projection into a local discriminated union. */
export function parseRequestResult(value: unknown): RequestResult | null {
  const raw = asObject(value);
  if (
    !raw ||
    hasInternalFields(raw) ||
    !nonBlank(raw.request_id) ||
    "type" in raw ||
    "tracking" in raw ||
    "text" in raw
  ) {
    return null;
  }

  const resultMarkers = ["answer_text", "kind", "reason_code", "error_code"].filter(
    (key) => key in raw
  );
  if (resultMarkers.length !== 1) return null;

  if ("answer_text" in raw) {
    const mode = answerMode(raw.mode);
    const sources = stringArray(raw.sources);
    const review = reviewStatus(raw.review_status);
    if (
      !nonBlank(raw.answer_text) ||
      !nonBlank(raw.record_id) ||
      !mode ||
      !sources ||
      !review ||
      !nonBlank(raw.answered_by) ||
      !nonBlank(raw.agent_id)
    ) {
      return null;
    }
    return {
      type: "answered",
      request_id: raw.request_id,
      record_id: raw.record_id,
      text: raw.answer_text,
      answered_by: { owner: raw.answered_by, agent_id: raw.agent_id },
      mode,
      sources,
      review_status: review,
    };
  }

  if ("kind" in raw) {
    const state = requestState(raw.state);
    const kind = raw.kind;
    if (
      typeof kind !== "string" ||
      !STREAM_PENDING_KINDS.has(kind as PendingKind) ||
      !state ||
      state === "answered" ||
      state === "declined" ||
      state === "failed" ||
      typeof raw.retryable !== "boolean" ||
      !nonBlank(raw.message)
    ) {
      return null;
    }
    return {
      type: "pending",
      request_id: raw.request_id,
      kind: kind as PendingKind,
      state,
      retryable: raw.retryable,
      message: raw.message,
    };
  }

  if ("reason_code" in raw && nonBlank(raw.reason_code) && nonBlank(raw.message)) {
    return {
      type: "declined",
      request_id: raw.request_id,
      reason_code: raw.reason_code,
      message: raw.message,
    };
  }

  if ("error_code" in raw && nonBlank(raw.error_code) && nonBlank(raw.message)) {
    return {
      type: "failed",
      request_id: raw.request_id,
      error_code: raw.error_code,
      message: raw.message,
    };
  }
  return null;
}

async function responseReply(res: Response): Promise<OrgReply> {
  const data: unknown = await res.json();
  const reply = parseOrgReply(data);
  if (!reply) throw new AskError("예상치 못한 응답 형식입니다.");
  return reply;
}

/** POST /api/ask — blocking Request-first result. */
export async function postAsk(question: string): Promise<OrgReply> {
  let res: Response;
  try {
    res = await fetch("/api/ask", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question }),
    });
  } catch {
    throw new AskError("백엔드에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.");
  }
  if (!res.ok) {
    throw new AskError(`질문 접수에 실패했습니다 (HTTP ${res.status}).`, res.status);
  }
  return responseReply(res);
}

/** GET /api/requests/{request_id} — native canonical request lookup. */
export async function getRequest(requestId: string): Promise<RequestResult | null> {
  let res: Response;
  try {
    res = await fetch(`/api/requests/${encodeURIComponent(requestId)}`, { method: "GET" });
  } catch {
    throw new AskError("질문 상태를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.");
  }
  if (res.status === 404) return null;
  if (!res.ok) {
    throw new AskError(`질문 상태 확인에 실패했습니다 (HTTP ${res.status}).`, res.status);
  }
  const headerRequestId = res.headers.get("x-request-id");
  const data: unknown = await res.json();
  const reply = parseRequestResult(data);
  if (!reply) throw new AskError("예상치 못한 질문 상태 형식입니다.");
  if (headerRequestId !== requestId || reply.request_id !== requestId) {
    throw new AskError("요청 ID가 다른 응답을 받았습니다.");
  }
  return reply;
}

const POLL_INTERVAL_MS = 700;
const POLL_MAX_ATTEMPTS = 30;

/** Poll the native canonical resource while the same request_id remains retryable. */
export async function pollRequest(requestId: string): Promise<RequestResult> {
  for (let attempt = 0; attempt < POLL_MAX_ATTEMPTS; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    const reply = await getRequest(requestId);
    if (reply === null) throw new AskError("질문 요청을 찾을 수 없습니다.", 404);
    if (reply.type !== "pending" || !reply.retryable) return reply;
  }
  throw new AskError("답변이 늦어지고 있습니다. 잠시 후 다시 확인해 주세요.");
}

/* ---- P17 sealed SSE contract ---- */

export interface AskAccepted {
  type: "accepted";
  request_id: string;
}
export interface AskToken {
  type: "token";
  request_id: string;
  text: string;
}
export interface AskPending {
  type: "pending";
  request_id: string;
  kind: PendingKind;
  state: Exclude<RequestState, "answered" | "declined" | "failed">;
  retryable: boolean;
  message: string;
}
export interface AskDone {
  type: "done";
  request_id: string;
  record_id: string;
  mode: AnswerMode;
  sources: string[];
  review_status: ReviewStatus;
  answered_by: string;
  agent_id: string;
}
export interface AskDeclined {
  type: "declined";
  request_id: string;
  reason_code: string;
  message: string;
}
export interface AskFailed {
  type: "failed";
  request_id: string;
  error_code: string;
  message: string;
}
export interface AskInterrupted {
  type: "interrupted";
  request_id: string;
  retryable: boolean;
  message: string;
}

export type AskStreamEvent =
  | AskAccepted
  | AskToken
  | AskPending
  | AskDone
  | AskDeclined
  | AskFailed
  | AskInterrupted;

export interface AskStreamHandlers {
  onAccepted?: (event: AskAccepted) => void;
  onToken?: (event: AskToken) => void;
  onPending?: (event: AskPending) => void;
  onDone?: (event: AskDone) => void;
  onDeclined?: (event: AskDeclined) => void;
  onFailed?: (event: AskFailed) => void;
  onInterrupted?: (event: AskInterrupted) => void;
}

/** Pure parser for one P17 SSE frame; comments/keep-alives return null. */
export function parseAskSseFrame(frame: string): AskStreamEvent | null {
  let eventName = "";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!eventName && dataLines.length === 0) return null;
  if (!eventName || dataLines.length === 0) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(dataLines.join("\n"));
  } catch {
    return null;
  }
  const raw = asObject(parsed);
  if (!raw || hasInternalFields(raw) || !nonBlank(raw.request_id)) return null;
  const requestId = raw.request_id;

  if (eventName === "accepted") return { type: "accepted", request_id: requestId };
  if (eventName === "token" && typeof raw.text === "string" && raw.text.length > 0) {
    return { type: "token", request_id: requestId, text: raw.text };
  }
  if (eventName === "pending") {
    const state = requestState(raw.state);
    const kind = raw.kind;
    if (
      typeof kind === "string" &&
      STREAM_PENDING_KINDS.has(kind as PendingKind) &&
      state &&
      state !== "answered" &&
      state !== "declined" &&
      state !== "failed" &&
      typeof raw.retryable === "boolean" &&
      nonBlank(raw.message)
    ) {
      return {
        type: "pending",
        request_id: requestId,
        kind: kind as PendingKind,
        state,
        retryable: raw.retryable,
        message: raw.message,
      };
    }
    return null;
  }
  if (eventName === "done") {
    const mode = answerMode(raw.mode);
    const sources = stringArray(raw.sources);
    const review = reviewStatus(raw.review_status);
    if (
      nonBlank(raw.record_id) &&
      mode &&
      sources &&
      review &&
      nonBlank(raw.answered_by) &&
      nonBlank(raw.agent_id)
    ) {
      return {
        type: "done",
        request_id: requestId,
        record_id: raw.record_id,
        mode,
        sources,
        review_status: review,
        answered_by: raw.answered_by,
        agent_id: raw.agent_id,
      };
    }
    return null;
  }
  if (eventName === "declined" && nonBlank(raw.reason_code) && nonBlank(raw.message)) {
    return {
      type: "declined",
      request_id: requestId,
      reason_code: raw.reason_code,
      message: raw.message,
    };
  }
  if (eventName === "failed" && nonBlank(raw.error_code) && nonBlank(raw.message)) {
    return {
      type: "failed",
      request_id: requestId,
      error_code: raw.error_code,
      message: raw.message,
    };
  }
  if (eventName === "interrupted" && typeof raw.retryable === "boolean" && nonBlank(raw.message)) {
    return {
      type: "interrupted",
      request_id: requestId,
      retryable: raw.retryable,
      message: raw.message,
    };
  }
  return null;
}

function dispatchStreamEvent(event: AskStreamEvent, handlers: AskStreamHandlers): void {
  switch (event.type) {
    case "accepted":
      handlers.onAccepted?.(event);
      break;
    case "token":
      handlers.onToken?.(event);
      break;
    case "pending":
      handlers.onPending?.(event);
      break;
    case "done":
      handlers.onDone?.(event);
      break;
    case "declined":
      handlers.onDeclined?.(event);
      break;
    case "failed":
      handlers.onFailed?.(event);
      break;
    case "interrupted":
      handlers.onInterrupted?.(event);
      break;
  }
}

function connectionEnds(event: AskStreamEvent): boolean {
  return event.type === "pending" || event.type === "done" || event.type === "declined" || event.type === "failed" || event.type === "interrupted";
}

/** POST /api/ask/stream — consume exact-linked P17 SSE events. */
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
    throw new AskError("백엔드에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.");
  }
  if (!res.ok || !res.body) {
    throw new AskError(`질문 스트림을 열지 못했습니다 (HTTP ${res.status}).`, res.status);
  }
  const headerRequestId = res.headers.get("x-request-id");
  if (!nonBlank(headerRequestId)) throw new AskError("응답에 요청 ID가 없습니다.");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let acceptedRequestId: string | null = null;
  let ended = false;

  const consume = (frame: string): void => {
    if (!frame.trim() || frame.trimStart().startsWith(":")) return;
    const event = parseAskSseFrame(frame);
    if (!event) throw new AskError("질문 스트림 형식이 올바르지 않습니다.");
    if (event.type === "accepted") {
      if (acceptedRequestId !== null || event.request_id !== headerRequestId) {
        throw new AskError("질문 스트림의 요청 ID가 일치하지 않습니다.");
      }
      acceptedRequestId = event.request_id;
    } else if (acceptedRequestId === null || event.request_id !== acceptedRequestId || ended) {
      throw new AskError("질문 스트림의 요청 ID가 일치하지 않습니다.");
    }
    dispatchStreamEvent(event, handlers);
    if (connectionEnds(event)) ended = true;
  };

  try {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let separator: number;
      while ((separator = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);
        consume(frame);
      }
      if (ended) {
        if (buffer.trim()) {
          throw new AskError("완료된 질문 스트림 뒤에 추가 데이터가 있습니다.");
        }
        break;
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) consume(buffer);
    if (acceptedRequestId === null || !ended) {
      throw new AskError("질문 스트림이 완료되기 전에 연결이 끝났습니다.");
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      // The transport may already be closed; releasing the reader is still required.
    }
    reader.releaseLock();
  }
}

/* ---- UI mapping helpers ---- */

export const modeMeta: Record<AnswerMode, { label: string; tone: StatusTone }> = {
  full: { label: "확정 답변", tone: "success" },
  draft_only: { label: "승인 대기", tone: "warning" },
  backup: { label: "백업 답변", tone: "info" },
};

export const reviewStatusLabel: Record<ReviewStatus, string> = {
  not_required: "별도 검토 불필요",
  approved: "검토 완료",
};

export function pendingTraceLabel(kind: PendingKind | LegacyPendingKind): string {
  switch (kind) {
    case "unowned":
      return "담당을 정하는 중";
    case "routing":
      return "담당을 찾는 중";
    case "routed":
    case "dispatched":
      return "담당에게 전달됨";
    case "contested":
      return "담당을 확인하는 중";
  }
}

export function pendingUserMessage(
  event: AskPending | PendingReply | RequestPendingResult
): string {
  if (event.kind === "unowned") return "담당을 지정하고 있습니다. 답변은 아직 준비되지 않았습니다.";
  if (event.kind === "contested") return "담당 범위를 확인하고 있습니다. 답변은 아직 준비되지 않았습니다.";
  if (event.state === "awaiting_approval") return "답변을 검토하고 있습니다. 승인 전에는 본문을 표시하지 않습니다.";
  return "질문을 처리하고 있습니다. 답변이 준비되면 같은 요청 ID로 확인할 수 있습니다.";
}
