/** Dedicated, fixed Central Question lifecycle browser relay. */

export type CentralQuestionRoute = "create" | "stream" | "retrieve" | "feedback";

type Upstream = { method: "GET" | "POST"; path: (requestId?: string) => string };
type Json = Record<string, unknown>;
type FetchLike = (input: URL, init: RequestInit) => Promise<Response>;

const CENTRAL_QUESTION_UPSTREAM = "http://127.0.0.1:8010";
const MAX_BODY_BYTES = 65_536;
const MAX_RESPONSE_BYTES = 1_048_576;
const UPSTREAM_TIMEOUT_MS = 10_000;
const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const CURSOR = /^[1-9][0-9]{0,18}$/;
const PROVENANCE_HEADER = "x-aon-admission-proxy-provenance";
const CLEAN_PROVENANCE = "next-standalone-clean";
const FORWARDED_HEADERS = new Set(["x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"]);
const SELF_CLAIM_HEADERS = new Set([
  "authorization", "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
  "x-user", "x-org", "x-owner", "x-role", "x-permission", "x-token", "x-token-claim",
  "x-aon-user", "x-aon-org", "x-aon-owner", "x-aon-role", "x-aon-permission", "x-aon-token",
  "x-aon-token-claim", "x-aon-session", "x-aon-actor", "x-aon-authority",
  "x-aon-question-proxy-provenance", "x-aon-internal",
]);
const UPSTREAM: Record<CentralQuestionRoute, Upstream> = {
  create: { method: "POST", path: () => "/v1/questions" },
  stream: { method: "GET", path: (id) => `/v1/questions/${id ?? ""}/stream` },
  retrieve: { method: "GET", path: (id) => `/v1/questions/${id ?? ""}` },
  feedback: { method: "POST", path: (id) => `/v1/questions/${id ?? ""}/feedback` },
};
const SAFE_ERRORS: Record<number, ReadonlySet<string>> = {
  401: new Set(["browser_session_unauthenticated"]),
  403: new Set(["browser_csrf_forbidden", "browser_session_forbidden", "question_forbidden"]),
  404: new Set(["question_not_found"]),
  409: new Set(["question_request_conflict", "question_feedback_conflict"]),
  422: new Set(["invalid_question_request", "invalid_question_stream_request", "invalid_question_feedback"]),
  503: new Set(["question_lifecycle_unavailable"]),
};
const KOREAN_ERROR: Record<string, string> = {
  browser_session_unauthenticated: "로그인이 필요하거나 세션이 만료되었습니다.",
  browser_session_forbidden: "현재 브라우저 세션으로 이 요청을 처리할 수 없습니다.",
  browser_csrf_forbidden: "요청 보안 검증에 실패했습니다.",
  question_forbidden: "이 질문 요청을 처리할 권한이 없습니다.",
  question_not_found: "질문 요청을 찾을 수 없습니다.",
  question_request_conflict: "같은 요청 키로 다른 질문을 보낼 수 없습니다.",
  question_feedback_conflict: "같은 요청 키로 다른 피드백을 보낼 수 없습니다.",
  invalid_question_request: "질문 요청 형식이 올바르지 않습니다.",
  invalid_question_stream_request: "질문 스트림 요청 형식이 올바르지 않습니다.",
  invalid_question_feedback: "피드백 요청 형식이 올바르지 않습니다.",
  question_lifecycle_unavailable: "질문 서비스를 지금 사용할 수 없습니다.",
};

export function questionUpstreamUrl(route: CentralQuestionRoute, requestId?: string): URL {
  if ((route === "stream" || route === "retrieve" || route === "feedback") && !isRequestId(requestId)) {
    throw new RangeError("invalid question request id");
  }
  return new URL(`${CENTRAL_QUESTION_UPSTREAM}${UPSTREAM[route].path(requestId)}`);
}

export async function isValidCentralQuestionRequest(
  route: CentralQuestionRoute, request: Request, publicOrigin: string, requestId?: string,
): Promise<boolean> {
  return (await validateCentralQuestionRequest(route, request, publicOrigin, requestId)) === "valid";
}

type RequestValidity = "valid" | "forbidden" | "invalid";

async function validateCentralQuestionRequest(
  route: CentralQuestionRoute, request: Request, publicOrigin: string, requestId?: string,
): Promise<RequestValidity> {
  try {
    const descriptor = UPSTREAM[route];
    const url = new URL(request.url);
    if (request.method !== descriptor.method || url.search || !hasExpectedPath(route, url.pathname, requestId)) return "forbidden";
    if (!hasExpectedHost(request.headers, publicOrigin) || !hasBrowserSessionCookie(request.headers) || hasCallerClaim(request.headers)) return "forbidden";
    if (route === "stream") {
      const cursor = request.headers.get("last-event-id");
      return request.headers.get("accept") === "text/event-stream" && (cursor === null || CURSOR.test(cursor)) && await hasEmptyBody(request) ? "valid" : "forbidden";
    }
    if (request.method === "GET") return await hasEmptyBody(request) ? "valid" : "forbidden";
    if (!hasExpectedPostEnvelope(request.headers, publicOrigin)) return "forbidden";
    await readCentralQuestionBody(route, request.clone());
    return "valid";
  } catch {
    return "invalid";
  }
}

export function centralQuestionRequestHeaders(route: CentralQuestionRoute, source: Headers): Headers {
  const result = new Headers();
  const names = route === "stream"
    ? ["cookie", "accept", "last-event-id"]
    : route === "retrieve"
      ? ["cookie"]
      : ["cookie", "origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "x-aon-csrf", "idempotency-key", "content-type"];
  for (const name of names) {
    const value = source.get(name);
    if (value !== null) result.set(name, value);
  }
  return result;
}

/** Read raw UTF-8 bytes only after checking the exact public write DTO. */
export async function readCentralQuestionBody(route: CentralQuestionRoute, request: Request): Promise<Uint8Array> {
  if (route !== "create" && route !== "feedback") throw new TypeError("question route has no write body");
  const bytes = await readBoundedRequest(request, MAX_BODY_BYTES);
  const text = decodeUtf8(bytes);
  let body: unknown;
  try { body = JSON.parse(text); } catch { throw new TypeError("invalid question json"); }
  if (!isObject(body)) throw new TypeError("invalid question dto");
  if (route === "create") {
    if (!hasExactKeys(body, ["question"]) || !isSafeText(body.question, false, MAX_BODY_BYTES)) throw new TypeError("invalid question dto");
  } else if (
    !hasExactKeys(body, ["record_id", "verdict", "comment"]) ||
    !isRequestId(body.record_id) || (body.verdict !== "good" && body.verdict !== "bad") || !isSafeText(body.comment, true, 4096)
  ) throw new TypeError("invalid feedback dto");
  return bytes;
}

export async function handleCentralQuestionBff(
  route: CentralQuestionRoute,
  request: Request,
  publicOrigin: string,
  requestId?: string,
  fetcher: FetchLike = fetch,
): Promise<Response> {
  const validity = await validateCentralQuestionRequest(route, request, publicOrigin, requestId);
  if (validity === "forbidden") return questionForbidden(route);
  if (validity === "invalid") return questionInvalid(route);
  let body: Uint8Array | undefined;
  try {
    body = route === "create" || route === "feedback" ? await readCentralQuestionBody(route, request) : undefined;
  } catch {
    return questionInvalid(route);
  }
  const controller = new AbortController();
  const abortFromBrowser = () => controller.abort();
  if (request.signal.aborted) controller.abort();
  else request.signal.addEventListener("abort", abortFromBrowser, { once: true });
  const timeout = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
  try {
    const upstream = await fetcher(questionUpstreamUrl(route, requestId), {
      method: request.method,
      headers: centralQuestionRequestHeaders(route, request.headers),
      body: body as unknown as BodyInit | undefined,
      redirect: "manual",
      cache: "no-store",
      signal: controller.signal,
    });
    return relayCentralQuestionResponse(route, requestId, upstream);
  } catch {
    return questionUnavailable();
  } finally {
    clearTimeout(timeout);
    request.signal.removeEventListener("abort", abortFromBrowser);
  }
}

export async function relayCentralQuestionResponse(
  route: CentralQuestionRoute, requestId: string | undefined, upstream: Response,
): Promise<Response> {
  if (route === "stream" && upstream.status === 200) {
    const contentType = upstream.headers.get("content-type") ?? "";
    if (!/^text\/event-stream(?:\s*;\s*charset=utf-8)?$/i.test(contentType) || upstream.body === null) return questionUnavailable();
    return new Response(upstream.body, {
      status: 200,
      headers: {
        "content-type": "text/event-stream; charset=utf-8",
        "cache-control": "no-cache, no-transform",
        "x-accel-buffering": "no",
      },
    });
  }
  if (SAFE_ERRORS[upstream.status] !== undefined) return relaySafeError(upstream);
  const expectedStatus = route === "retrieve" ? 200 : 201;
  if (upstream.status !== expectedStatus) return questionUnavailable();
  try {
    const value = parseJson(decodeUtf8(await readBoundedResponse(upstream, MAX_RESPONSE_BYTES)));
    if (!isSafeSuccess(route, requestId, value)) return questionUnavailable();
    return json(value, upstream.status);
  } catch {
    return questionUnavailable();
  }
}

export function questionUnavailable(): Response {
  return json({ error: "central_question_unavailable", message: "질문 서비스를 지금 사용할 수 없습니다." }, 502);
}

function questionForbidden(route: CentralQuestionRoute): Response {
  const status = route === "create" || route === "feedback" ? 403 : 400;
  return json({ error: "central_question_forbidden", message: "질문 요청 보안 검증에 실패했습니다." }, status);
}

function questionInvalid(route: CentralQuestionRoute): Response {
  const error = route === "feedback" ? "invalid_question_feedback" : "invalid_question_request";
  return json({ error, message: KOREAN_ERROR[error] }, 422);
}

async function relaySafeError(upstream: Response): Promise<Response> {
  try {
    const value = parseJson(decodeUtf8(await readBoundedResponse(upstream, MAX_RESPONSE_BYTES)));
    if (!isObject(value) || !hasExactKeys(value, ["error"]) || typeof value.error !== "string" || !SAFE_ERRORS[upstream.status].has(value.error)) return questionUnavailable();
    return json({ error: value.error, message: KOREAN_ERROR[value.error] }, upstream.status);
  } catch {
    return questionUnavailable();
  }
}

function isSafeSuccess(route: CentralQuestionRoute, requestId: string | undefined, value: unknown): value is Json {
  if (!isObject(value)) return false;
  if (route === "create") return hasExactKeys(value, ["request_id", "state", "created_at", "replayed"]) && isRequestId(value.request_id) && value.state === "received" && typeof value.created_at === "string" && typeof value.replayed === "boolean";
  if (route === "feedback") return hasExactKeys(value, ["request_id", "record_id", "feedback_id", "verdict", "submitted_at", "replayed"]) && value.request_id === requestId && isRequestId(value.record_id) && isRequestId(value.feedback_id) && (value.verdict === "good" || value.verdict === "bad") && typeof value.submitted_at === "string" && typeof value.replayed === "boolean";
  if (value.request_id !== requestId || typeof value.type !== "string" || typeof value.state !== "string" || typeof value.retryable !== "boolean") return false;
  if (value.type === "pending") return hasExactKeys(value, ["type", "request_id", "state", "kind", "retryable", "message"]) && typeof value.kind === "string" && typeof value.message === "string";
  if (value.type === "declined") return hasExactKeys(value, ["type", "request_id", "state", "retryable", "reason_code", "message"]) && value.state === "declined" && value.retryable === false && typeof value.reason_code === "string" && typeof value.message === "string";
  if (value.type === "failed") return hasExactKeys(value, ["type", "request_id", "state", "retryable", "error_code", "message"]) && value.state === "failed" && value.retryable === false && typeof value.error_code === "string" && typeof value.message === "string";
  if (value.type !== "answered" || !hasExactKeys(value, ["type", "request_id", "state", "retryable", "record_id", "text", "answered_by", "mode", "sources", "review_status"])) return false;
  return value.state === "answered" && value.retryable === false && isRequestId(value.record_id) && typeof value.text === "string" && isAnsweredBy(value.answered_by) && (value.mode === "full" || value.mode === "backup") && Array.isArray(value.sources) && value.sources.every((source) => typeof source === "string") && (value.review_status === "not_required" || value.review_status === "approved");
}

function isAnsweredBy(value: unknown): boolean {
  return isObject(value) && hasExactKeys(value, ["owner", "agent_id"]) && typeof value.owner === "string" && typeof value.agent_id === "string";
}

function hasExpectedPath(route: CentralQuestionRoute, pathname: string, requestId?: string): boolean {
  if ((route === "stream" || route === "retrieve" || route === "feedback") && !isRequestId(requestId)) return false;
  return pathname === `/api/questions${route === "create" ? "" : `/${requestId}${route === "stream" ? "/stream" : route === "feedback" ? "/feedback" : ""}`}`;
}

function hasExpectedPostEnvelope(headers: Headers, publicOrigin: string): boolean {
  if (headers.get("origin") !== publicOrigin || headers.get("sec-fetch-site") !== "same-origin" || headers.get("sec-fetch-mode") !== "cors" || headers.get("sec-fetch-dest") !== "empty") return false;
  if (!isOpaque(headers.get("x-aon-csrf"), 32, 128) || !isIdempotencyKey(headers.get("idempotency-key"))) return false;
  return /^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(headers.get("content-type") ?? "");
}

function hasCallerClaim(headers: Headers): boolean {
  const provenance = headers.get(PROVENANCE_HEADER);
  if (provenance !== null && provenance !== CLEAN_PROVENANCE) return true;
  const trustedForwarding = provenance === CLEAN_PROVENANCE;
  for (const name of Array.from(headers.keys()).map((value) => value.toLowerCase())) {
    if (name === PROVENANCE_HEADER) continue;
    if (SELF_CLAIM_HEADERS.has(name)) {
      if (trustedForwarding && FORWARDED_HEADERS.has(name)) continue;
      return true;
    }
    if (name.startsWith("x-aon-") && name !== "x-aon-csrf") return true;
    if (name.startsWith("x-forwarded-") && !(trustedForwarding && FORWARDED_HEADERS.has(name))) return true;
  }
  return false;
}

function hasExpectedHost(headers: Headers, publicOrigin: string): boolean {
  const host = headers.get("host");
  if (host === null || /[\s,]/.test(host)) return false;
  try {
    if (new URL(`http://${host}`).host === new URL(publicOrigin).host) return true;
    return process.env.HOSTNAME !== undefined && process.env.PORT !== undefined && host === `${process.env.HOSTNAME}:${process.env.PORT}`;
  } catch { return false; }
}

function hasBrowserSessionCookie(headers: Headers): boolean {
  const cookie = headers.get("cookie");
  return cookie !== null && /(?:^|;\s*)__Host-aon-central-session=[A-Za-z0-9_-]+(?:;|$)/.test(cookie);
}

function isRequestId(value: unknown): value is string { return typeof value === "string" && REQUEST_ID.test(value); }
function isIdempotencyKey(value: string | null): boolean { return value !== null && IDEMPOTENCY_KEY.test(value); }
function isOpaque(value: string | null, minimum: number, maximum: number): boolean { return value !== null && /^[A-Za-z0-9_-]+$/.test(value) && byteLength(value) >= minimum && byteLength(value) <= maximum; }
function isObject(value: unknown): value is Json { return typeof value === "object" && value !== null && !Array.isArray(value); }
function hasExactKeys(value: Json, keys: readonly string[]): boolean { return Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key)); }
function isSafeText(value: unknown, empty: boolean, maximum: number): value is string { return typeof value === "string" && (empty || value.trim().length > 0) && !hasLoneSurrogate(value) && byteLength(value) <= maximum; }
function hasLoneSurrogate(value: string): boolean { for (let index = 0; index < value.length; index += 1) { const unit = value.charCodeAt(index); if (unit >= 0xd800 && unit <= 0xdbff) { if (index + 1 >= value.length || value.charCodeAt(index + 1) < 0xdc00 || value.charCodeAt(index + 1) > 0xdfff) return true; index += 1; } else if (unit >= 0xdc00 && unit <= 0xdfff) return true; } return false; }
function byteLength(value: string): number { return new TextEncoder().encode(value).byteLength; }
function decodeUtf8(value: Uint8Array): string { return new TextDecoder("utf-8", { fatal: true }).decode(value); }
function parseJson(value: string): unknown { return JSON.parse(value); }
function json(value: Json, status: number): Response { return new Response(JSON.stringify(value), { status, headers: { "cache-control": "no-store", "content-type": "application/json; charset=utf-8" } }); }

async function hasEmptyBody(request: Request): Promise<boolean> { const declared = request.headers.get("content-length"); if (declared !== null && declared !== "0") return false; if (request.body === null) return true; const reader = request.body.getReader(); try { while (true) { const next = await reader.read(); if (next.done) return true; if (next.value.byteLength !== 0) return false; } } finally { reader.releaseLock(); } }
async function readBoundedRequest(request: Request, maximum: number): Promise<Uint8Array> { const declared = request.headers.get("content-length"); if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > maximum)) throw new RangeError("question body too large"); if (request.body === null) return new Uint8Array(); const reader = request.body.getReader(); const chunks: Uint8Array[] = []; let length = 0; try { while (true) { const next = await reader.read(); if (next.done) break; length += next.value.byteLength; if (length > maximum) throw new RangeError("question body too large"); chunks.push(next.value); } } finally { reader.releaseLock(); } return join(chunks, length); }
async function readBoundedResponse(response: Response, maximum: number): Promise<Uint8Array> { const declared = response.headers.get("content-length"); if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > maximum)) throw new RangeError("question response too large"); if (response.body === null) return new Uint8Array(); const reader = response.body.getReader(); const chunks: Uint8Array[] = []; let length = 0; try { while (true) { const next = await reader.read(); if (next.done) break; length += next.value.byteLength; if (length > maximum) throw new RangeError("question response too large"); chunks.push(next.value); } } finally { reader.releaseLock(); } return join(chunks, length); }
function join(chunks: readonly Uint8Array[], length: number): Uint8Array { const result = new Uint8Array(length); let offset = 0; for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.byteLength; } return result; }
