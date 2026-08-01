/** Browser client for the sealed Central Question lifecycle. */

export type PendingState = "received" | "ready_to_dispatch" | "awaiting_answer" | "awaiting_approval" | "awaiting_conflict" | "awaiting_manager";
export type PendingKind = "routing" | "routed" | "contested" | "unowned";
export type AnswerMode = "full" | "backup";
export type ReviewStatus = "not_required" | "approved";

export type ReceivedQuestion = { request_id: string; state: "received"; created_at: string; replayed: boolean };
export type PendingProjection = { type: "pending"; state: PendingState; kind: PendingKind; retryable: boolean; request_id: string; message: string };
export type AnsweredProjection = { type: "answered"; state: "answered"; retryable: false; request_id: string; record_id: string; text: string; answered_by: { owner: string; agent_id: string }; mode: AnswerMode; sources: string[]; review_status: ReviewStatus };
export type DeclinedProjection = { type: "declined"; state: "declined"; retryable: false; request_id: string; reason_code: string; message: string };
export type FailedProjection = { type: "failed"; state: "failed"; retryable: false; request_id: string; error_code: string; message: string };
export type QuestionProjection = PendingProjection | AnsweredProjection | DeclinedProjection | FailedProjection;
/** `interrupted` has no server message: denial/unavailability meaning is local UI copy. */
export type InterruptedEvent = { type: "interrupted"; request_id: string; retryable: boolean };
export type TokenEvent = { type: "token"; request_id: string; text: string };
export type QuestionStreamEvent = { type: "accepted"; request_id: string } | TokenEvent | { type: "pending"; event: PendingProjection } | { type: "done"; event: AnsweredProjection } | { type: "declined"; event: DeclinedProjection } | { type: "failed"; event: FailedProjection } | InterruptedEvent;
export type DecodedStreamEvent = QuestionStreamEvent & { cursor: string };
export type FeedbackInput = { record_id: string; verdict: "good" | "bad"; comment: string };
export type FeedbackReceipt = { request_id: string; record_id: string; feedback_id: string; verdict: "good" | "bad"; submitted_at: string; replayed: boolean };
export type QuestionEventSource = { addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void; close(): void; onerror?: ((event: Event) => void) | null };
export type QuestionSubscriptionHandlers = { onEvent(event: QuestionStreamEvent): void; onFault(error: QuestionClientError): void; onReconnect?(): void };

export class QuestionClientError extends Error {
  constructor(message: string, readonly status?: number, readonly retryable = false) { super(message); this.name = "QuestionClientError"; }
}

const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const CURSOR = /^[1-9][0-9]{0,18}$/;
const PENDING_STATES = new Set<PendingState>(["received", "ready_to_dispatch", "awaiting_answer", "awaiting_approval", "awaiting_conflict", "awaiting_manager"]);
const PENDING_KINDS = new Set<PendingKind>(["routing", "routed", "contested", "unowned"]);
const ERROR_MESSAGES: Record<number, string> = {
  401: "조직 SSO 세션이 필요합니다.", 403: "현재 권한으로 질문을 처리할 수 없습니다.", 404: "질문 요청을 찾을 수 없습니다.",
  409: "같은 요청 키로 다른 내용을 보낼 수 없습니다.", 422: "질문 요청 형식이 올바르지 않습니다.",
  502: "질문 서비스를 지금 연결할 수 없습니다.", 503: "질문 서비스를 지금 사용할 수 없습니다.",
};

function object(value: unknown): Record<string, unknown> | null { return typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : null; }
function exact(value: Record<string, unknown>, keys: readonly string[]): boolean { return Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key)); }
function id(value: unknown): value is string { return typeof value === "string" && REQUEST_ID.test(value); }
function text(value: unknown, blank = false): value is string { return typeof value === "string" && (blank || value.trim().length > 0) && !loneSurrogate(value); }
function loneSurrogate(value: string): boolean { for (let i = 0; i < value.length; i += 1) { const code = value.charCodeAt(i); if (code >= 0xd800 && code <= 0xdbff) { if (i + 1 === value.length || value.charCodeAt(i + 1) < 0xdc00 || value.charCodeAt(i + 1) > 0xdfff) return true; i += 1; } else if (code >= 0xdc00 && code <= 0xdfff) return true; } return false; }
function bytes(value: string): number { return new TextEncoder().encode(value).byteLength; }
function cookie(name: string): string | null { if (typeof document === "undefined") return null; const prefix = `${name}=`; const found = document.cookie.split(";").map((item) => item.trim()).find((item) => item.startsWith(prefix)); if (!found) return null; try { return decodeURIComponent(found.slice(prefix.length)); } catch { return null; } }
function errorFor(status: number): QuestionClientError { return new QuestionClientError(ERROR_MESSAGES[status] ?? "질문 요청을 안전하게 처리하지 못했습니다.", status, status === 502 || status === 503); }
function randomKey(): string { return `${Date.now().toString(36)}-${crypto.getRandomValues(new Uint32Array(2)).join("")}`; }

export function decodeReceivedQuestion(value: unknown): ReceivedQuestion | null {
  const raw = object(value);
  return raw && exact(raw, ["request_id", "state", "created_at", "replayed"]) && id(raw.request_id) && raw.state === "received" && typeof raw.created_at === "string" && !Number.isNaN(Date.parse(raw.created_at)) && typeof raw.replayed === "boolean"
    ? raw as ReceivedQuestion : null;
}

export function decodeQuestionProjection(value: unknown): QuestionProjection | null {
  const raw = object(value);
  if (!raw || !id(raw.request_id) || typeof raw.type !== "string") return null;
  if (raw.type === "pending") {
    if (!exact(raw, ["type", "state", "kind", "retryable", "request_id", "message"]) || !PENDING_STATES.has(raw.state as PendingState) || !PENDING_KINDS.has(raw.kind as PendingKind) || typeof raw.retryable !== "boolean" || !text(raw.message)) return null;
    const legal = (raw.state === "received" && raw.kind === "routing" && raw.retryable) || (raw.state === "ready_to_dispatch" && raw.kind === "routed" && raw.retryable) || (raw.state === "awaiting_answer" && raw.kind === "routed" && raw.retryable) || (raw.state === "awaiting_approval" && raw.kind === "routed" && !raw.retryable) || (raw.state === "awaiting_conflict" && raw.kind === "contested" && !raw.retryable) || (raw.state === "awaiting_manager" && (raw.kind === "unowned" || raw.kind === "contested" || raw.kind === "routed") && !raw.retryable);
    return legal ? raw as PendingProjection : null;
  }
  if (raw.type === "answered") {
    const attribution = object(raw.answered_by);
    if (!exact(raw, ["type", "state", "retryable", "request_id", "record_id", "text", "answered_by", "mode", "sources", "review_status"]) || raw.state !== "answered" || raw.retryable !== false || !id(raw.record_id) || !text(raw.text) || !attribution || !exact(attribution, ["owner", "agent_id"]) || !text(attribution.owner) || !text(attribution.agent_id) || (raw.mode !== "full" && raw.mode !== "backup") || !Array.isArray(raw.sources) || !raw.sources.every((source) => text(source)) || (raw.review_status !== "not_required" && raw.review_status !== "approved")) return null;
    return raw as AnsweredProjection;
  }
  if (raw.type === "declined" && exact(raw, ["type", "state", "retryable", "request_id", "reason_code", "message"]) && raw.state === "declined" && raw.retryable === false && text(raw.reason_code) && text(raw.message)) return raw as DeclinedProjection;
  if (raw.type === "failed" && exact(raw, ["type", "state", "retryable", "request_id", "error_code", "message"]) && raw.state === "failed" && raw.retryable === false && text(raw.error_code) && text(raw.message)) return raw as FailedProjection;
  return null;
}

export function decodeQuestionStreamEvent(event: { type: string; data: string; lastEventId: string }, requestId: string): DecodedStreamEvent | null {
  if (!CURSOR.test(event.lastEventId)) return null;
  let payload: unknown;
  try { payload = JSON.parse(event.data); } catch { return null; }
  const raw = object(payload);
  if (!raw || !id(raw.request_id) || raw.request_id !== requestId) return null;
  if (event.type === "accepted" && exact(raw, ["request_id"])) return { type: "accepted", request_id: raw.request_id, cursor: event.lastEventId };
  if (event.type === "token" && exact(raw, ["request_id", "text"]) && text(raw.text, true)) return { type: "token", request_id: raw.request_id, text: raw.text, cursor: event.lastEventId };
  if (event.type === "interrupted" && exact(raw, ["request_id", "retryable"]) && typeof raw.retryable === "boolean") return { type: "interrupted", request_id: raw.request_id, retryable: raw.retryable, cursor: event.lastEventId };
  const projection = decodeQuestionProjection(payload);
  if (!projection) return null;
  if (event.type === "pending" && projection.type === "pending") return { type: "pending", event: projection, cursor: event.lastEventId };
  if (event.type === "done" && projection.type === "answered") return { type: "done", event: projection, cursor: event.lastEventId };
  if (event.type === "declined" && projection.type === "declined") return { type: "declined", event: projection, cursor: event.lastEventId };
  if (event.type === "failed" && projection.type === "failed") return { type: "failed", event: projection, cursor: event.lastEventId };
  return null;
}

async function json(response: Response): Promise<unknown> { try { return await response.json(); } catch { throw new QuestionClientError("질문 응답 형식이 올바르지 않습니다.", response.status); } }
function postHeaders(idempotencyKey: string): Record<string, string> {
  const csrf = cookie("__Host-aon-central-csrf");
  if (!csrf || !IDEMPOTENCY_KEY.test(idempotencyKey)) throw new QuestionClientError("브라우저 보안 확인을 완료할 수 없습니다.");
  return { "content-type": "application/json", "X-AON-CSRF": csrf, "Idempotency-Key": idempotencyKey };
}

export async function createQuestion(question: string, idempotencyKey = randomKey()): Promise<ReceivedQuestion> {
  if (!text(question) || bytes(question) > 65536) throw new QuestionClientError("질문을 입력해 주세요.");
  let response: Response;
  try { response = await fetch("/api/questions", { method: "POST", credentials: "same-origin", cache: "no-store", headers: postHeaders(idempotencyKey), body: JSON.stringify({ question }) }); } catch { throw new QuestionClientError("질문 서비스를 지금 연결할 수 없습니다.", undefined, true); }
  if (!response.ok) throw errorFor(response.status);
  const received = decodeReceivedQuestion(await json(response));
  if (!received) throw new QuestionClientError("질문 접수 응답 형식이 올바르지 않습니다.", response.status);
  return received;
}

export async function retrieveQuestion(requestId: string): Promise<QuestionProjection> {
  if (!id(requestId)) throw new QuestionClientError("질문 요청 식별자가 올바르지 않습니다.");
  let response: Response;
  try { response = await fetch(`/api/questions/${encodeURIComponent(requestId)}`, { credentials: "same-origin", cache: "no-store" }); } catch { throw new QuestionClientError("질문 상태를 지금 확인할 수 없습니다.", undefined, true); }
  if (!response.ok) throw errorFor(response.status);
  const result = decodeQuestionProjection(await json(response));
  if (!result || result.request_id !== requestId) throw new QuestionClientError("질문 요청 식별자가 일치하지 않습니다.");
  return result;
}

export async function submitFeedback(requestId: string, input: FeedbackInput, idempotencyKey = randomKey()): Promise<FeedbackReceipt> {
  if (!id(requestId) || !id(input.record_id) || (input.verdict !== "good" && input.verdict !== "bad") || typeof input.comment !== "string" || loneSurrogate(input.comment) || bytes(input.comment) > 4096) throw new QuestionClientError("피드백은 최대 4096 UTF-8 bytes까지 보낼 수 있습니다.");
  let response: Response;
  try { response = await fetch(`/api/questions/${encodeURIComponent(requestId)}/feedback`, { method: "POST", credentials: "same-origin", cache: "no-store", headers: postHeaders(idempotencyKey), body: JSON.stringify(input) }); } catch { throw new QuestionClientError("피드백 서비스를 지금 연결할 수 없습니다.", undefined, true); }
  if (!response.ok) throw errorFor(response.status);
  const raw = object(await json(response));
  if (!raw || !exact(raw, ["request_id", "record_id", "feedback_id", "verdict", "submitted_at", "replayed"]) || raw.request_id !== requestId || raw.record_id !== input.record_id || !id(raw.feedback_id) || raw.verdict !== input.verdict || typeof raw.submitted_at !== "string" || Number.isNaN(Date.parse(raw.submitted_at)) || typeof raw.replayed !== "boolean") throw new QuestionClientError("피드백 응답 형식이 올바르지 않습니다.", response.status);
  return raw as FeedbackReceipt;
}

export function lifecycleMessage(event: QuestionProjection | InterruptedEvent): string {
  if (event.type === "answered") return "답변이 확정되었습니다.";
  if (event.type === "declined") return event.message;
  if (event.type === "failed") return event.message;
  if (event.type === "interrupted") return event.retryable ? "연결이 끊겨 질문 상태를 다시 확인하고 있습니다." : "현재 권한으로 질문 상태를 계속 확인할 수 없습니다.";
  if (event.state === "received") return "질문을 접수했습니다. 담당을 찾고 있습니다.";
  if (event.state === "ready_to_dispatch") return "담당에게 전달할 준비를 하고 있습니다. 아직 답변은 확정되지 않았습니다.";
  if (event.state === "awaiting_answer") return "담당이 답변을 준비하고 있습니다. 아직 답변은 확정되지 않았습니다.";
  if (event.state === "awaiting_approval") return "답변을 검토하고 있습니다. 아직 확정되지 않았습니다.";
  if (event.state === "awaiting_conflict") return "담당 범위를 확인하고 있습니다. 아직 답변은 확정되지 않았습니다.";
  return "담당 처분을 기다리고 있습니다. 아직 답변은 확정되지 않았습니다.";
}

/**
 * EventSource owns its Last-Event-ID cursor across native reconnects.  We do
 * not manufacture a query cursor (the BFF intentionally rejects one).  After
 * four transport errors the subscription closes and asks the caller to
 * converge with canonical GET; backend execution is never cancelled.
 */
export function subscribeQuestion(
  requestId: string,
  handlers: QuestionSubscriptionHandlers,
  factory: (url: string) => QuestionEventSource = (url) => new EventSource(url),
): () => void {
  if (!id(requestId)) throw new QuestionClientError("질문 요청 식별자가 올바르지 않습니다.");
  let closed = false;
  let errors = 0;
  const source = factory(`/api/questions/${encodeURIComponent(requestId)}/stream`);
  const stop = (): void => { if (!closed) { closed = true; source.close(); } };
  const receive = (wire: MessageEvent<string>): void => {
    if (closed) return;
    const decoded = decodeQuestionStreamEvent({ type: wire.type, data: wire.data, lastEventId: wire.lastEventId }, requestId);
    if (!decoded) { stop(); handlers.onFault(new QuestionClientError("질문 스트림 응답을 안전하게 확인하지 못했습니다.")); return; }
    errors = 0;
    let event: QuestionStreamEvent;
    switch (decoded.type) {
      case "accepted": event = { type: "accepted", request_id: decoded.request_id }; break;
      case "token": event = { type: "token", request_id: decoded.request_id, text: decoded.text }; break;
      case "pending": event = { type: "pending", event: decoded.event }; break;
      case "done": event = { type: "done", event: decoded.event }; break;
      case "declined": event = { type: "declined", event: decoded.event }; break;
      case "failed": event = { type: "failed", event: decoded.event }; break;
      case "interrupted": event = { type: "interrupted", request_id: decoded.request_id, retryable: decoded.retryable }; break;
    }
    handlers.onEvent(event);
    if (event.type === "done" || event.type === "declined" || event.type === "failed" || (event.type === "interrupted" && !event.retryable)) stop();
  };
  for (const kind of ["accepted", "token", "pending", "done", "declined", "failed", "interrupted"]) source.addEventListener(kind, receive);
  source.onerror = () => {
    if (closed) return;
    errors += 1;
    if (errors > 4) { stop(); handlers.onFault(new QuestionClientError("연결이 반복해서 끊겼습니다. 질문 상태를 다시 확인해 주세요.", undefined, true)); return; }
    handlers.onReconnect?.();
  };
  return stop;
}
