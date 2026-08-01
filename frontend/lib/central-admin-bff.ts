/** Dedicated Central graph/ownership/organization-scorecard browser relay (ADR 0081). */

export type CentralAdminRoute = "org" | "owner-transfer" | "revoke" | "scorecard";

type Descriptor = {
  method: "GET" | "POST";
  suffix: string;
  publicPrefix: string;
  needsCard: boolean;
};
type Json = Record<string, unknown>;
type FetchLike = (input: URL, init: RequestInit) => Promise<Response>;

const CENTRAL = "http://127.0.0.1:8010";
const MAX_BODY = 65_536;
const MAX_RESPONSE = 1_048_576;
const TIMEOUT_MS = 10_000;
const REFERENCE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const REASON_CODE = /^[a-z0-9_]{1,64}$/;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const CSRF = /^[A-Za-z0-9_-]{32,128}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const RFC3339_UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;
const PROVENANCE = "x-aon-admission-proxy-provenance";
const PROOF_HEADER = "aon-standalone-provenance-proof";
const PROOF_SYMBOL = Symbol.for("agent-org-network.standalone-provenance-proof");
const CLEAN_PROVENANCE = "next-standalone-clean";
const FORWARDED = new Set(["x-forwarded-for", "x-forwarded-host", "x-forwarded-port", "x-forwarded-proto"]);
const SELF_CLAIMS = new Set([
  "authorization", "forwarded", "x-user", "x-org", "x-owner", "x-role", "x-permission",
  "x-token", "x-token-claim", "x-aon-user", "x-aon-org", "x-aon-owner", "x-aon-role",
  "x-aon-permission", "x-aon-token", "x-aon-token-claim", "x-aon-session", "x-aon-actor",
  "x-aon-authority", "x-aon-internal",
]);
const ROUTES: Record<CentralAdminRoute, Descriptor> = {
  org: { method: "GET", suffix: "/v1/console/org", publicPrefix: "/api/console/org", needsCard: false },
  "owner-transfer": { method: "POST", suffix: "/v1/admin/agent-cards/{card_id}/owner-transfers", publicPrefix: "/api/admin/agent-cards/{card_id}/owner-transfers", needsCard: true },
  revoke: { method: "POST", suffix: "/v1/admin/agent-cards/{card_id}/revocations", publicPrefix: "/api/admin/agent-cards/{card_id}/revocations", needsCard: true },
  scorecard: { method: "GET", suffix: "/v1/admin/scorecard", publicPrefix: "/api/admin/scorecard", needsCard: false },
};
const SAFE_ERRORS: Record<number, ReadonlySet<string>> = {
  401: new Set(["browser_session_unauthenticated", "session_unavailable"]),
  403: new Set(["browser_session_forbidden", "browser_csrf_forbidden", "admin_forbidden", "central_admin_forbidden", "card_forbidden", "org_graph_forbidden", "scorecard_forbidden"]),
  404: new Set(["not_found_or_denied", "card_not_found", "organization_not_found"]),
  409: new Set(["stale_or_conflict", "card_transfer_conflict", "card_revoke_conflict"]),
  422: new Set(["invalid_input", "invalid_card_transfer", "invalid_card_revoke", "invalid_scorecard_request"]),
  503: new Set(["unavailable", "central_admin_unavailable", "card_ownership_unavailable", "organization_scorecard_unavailable"]),
};
const MESSAGE: Record<string, string> = {
  browser_session_unauthenticated: "로그인이 필요하거나 세션이 만료되었습니다.",
  session_unavailable: "로그인이 필요하거나 세션이 만료되었습니다.",
  browser_session_forbidden: "현재 브라우저 세션으로 이 요청을 처리할 수 없습니다.",
  browser_csrf_forbidden: "요청 보안 검증에 실패했습니다.",
  admin_forbidden: "관리자 요청을 처리할 권한이 없습니다.",
  central_admin_forbidden: "관리자 요청을 처리할 권한이 없습니다.",
  card_forbidden: "카드 요청을 처리할 권한이 없습니다.",
  org_graph_forbidden: "조직 그래프를 볼 권한이 없습니다.",
  scorecard_forbidden: "조직 스코어카드를 볼 권한이 없습니다.",
  not_found_or_denied: "관리자 요청 대상을 찾을 수 없습니다.",
  card_not_found: "Agent Card를 찾을 수 없습니다.",
  organization_not_found: "조직을 찾을 수 없습니다.",
  stale_or_conflict: "대상이 변경되었습니다. 새로 고친 뒤 다시 시도해 주세요.",
  card_transfer_conflict: "Card Owner 전이가 충돌했습니다. 새로 고친 뒤 다시 시도해 주세요.",
  card_revoke_conflict: "Card Owner 해제가 충돌했습니다. 새로 고친 뒤 다시 시도해 주세요.",
  invalid_input: "관리자 요청 형식이 올바르지 않습니다.",
  invalid_card_transfer: "Card Owner 전이 요청 형식이 올바르지 않습니다.",
  invalid_card_revoke: "Card Owner 해제 요청 형식이 올바르지 않습니다.",
  invalid_scorecard_request: "조직 스코어카드 요청 형식이 올바르지 않습니다.",
  unavailable: "관리자 서비스를 지금 사용할 수 없습니다.",
  central_admin_unavailable: "관리자 서비스를 지금 사용할 수 없습니다.",
  card_ownership_unavailable: "Card Owner 서비스를 지금 사용할 수 없습니다.",
  organization_scorecard_unavailable: "조직 스코어카드 서비스를 지금 사용할 수 없습니다.",
};

export function centralAdminUpstreamUrl(route: CentralAdminRoute, cardId?: string, query?: URLSearchParams): URL {
  const descriptor = ROUTES[route];
  if (descriptor.needsCard !== isReference(cardId)) throw new RangeError("invalid admin card id");
  if (!descriptor.needsCard && cardId !== undefined) throw new RangeError("unexpected admin card id");
  const path = descriptor.suffix.replace("{card_id}", cardId ?? "");
  const result = new URL(`${CENTRAL}${path}`);
  if (route === "scorecard" && query !== undefined) {
    query.forEach((value, key) => result.searchParams.append(key, value));
  } else if (query !== undefined && Array.from(query.keys()).length > 0) throw new RangeError("unexpected admin query");
  return result;
}

export async function isValidCentralAdminRequest(
  route: CentralAdminRoute, request: Request, publicOrigin: string, cardId?: string,
): Promise<boolean> {
  return (await validate(route, request, publicOrigin, cardId)) === "valid";
}

export function centralAdminRequestHeaders(route: CentralAdminRoute, source: Headers): Headers {
  const result = new Headers();
  const names = ROUTES[route].method === "GET"
    ? ["cookie"]
    : ["cookie", "origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "x-aon-csrf", "idempotency-key", "content-type"];
  for (const name of names) { const value = source.get(name); if (value !== null) result.set(name, value); }
  return result;
}

export async function readCentralAdminBody(route: CentralAdminRoute, request: Request): Promise<Uint8Array> {
  if (ROUTES[route].method !== "POST") throw new TypeError("admin route has no body");
  const bytes = await boundedRequest(request, MAX_BODY);
  let value: unknown;
  try { value = JSON.parse(decodeUtf8(bytes)); } catch { throw new TypeError("invalid admin json"); }
  if (!isObject(value) || !validDto(route, value)) throw new TypeError("invalid admin dto");
  return bytes;
}

export async function handleCentralAdminBff(
  route: CentralAdminRoute, request: Request, publicOrigin: string, cardId?: string, fetcher: FetchLike = fetch,
): Promise<Response> {
  const validity = await validate(route, request, publicOrigin, cardId);
  if (validity === "forbidden") return safe({ code: "invalid_input", message: MESSAGE.invalid_input }, 403);
  if (validity === "invalid") return safe({ code: "invalid_input", message: MESSAGE.invalid_input }, 422);
  let body: Uint8Array | undefined;
  try { body = ROUTES[route].method === "POST" ? await readCentralAdminBody(route, request) : undefined; } catch { return safe({ code: errorForRoute(route), message: MESSAGE[errorForRoute(route)] }, 422); }
  const url = new URL(request.url);
  const query = route === "scorecard" ? url.searchParams : undefined;
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  if (request.signal.aborted) controller.abort(); else request.signal.addEventListener("abort", onAbort, { once: true });
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const upstream = await fetcher(centralAdminUpstreamUrl(route, cardId, query), {
      method: ROUTES[route].method,
      headers: centralAdminRequestHeaders(route, request.headers),
      body: body as unknown as BodyInit | undefined,
      redirect: "manual",
      cache: "no-store",
      signal: controller.signal,
    });
    return await relayCentralAdminResponse(route, upstream);
  } catch { return unavailable(); }
  finally { clearTimeout(timeout); request.signal.removeEventListener("abort", onAbort); }
}

export async function relayCentralAdminResponse(route: CentralAdminRoute, upstream: Response): Promise<Response> {
  if (SAFE_ERRORS[upstream.status] !== undefined) return relayError(upstream);
  const expectedStatus = route === "owner-transfer" || route === "revoke" ? 201 : 200;
  if (upstream.status !== expectedStatus) return unavailable();
  try {
    const value = parseJson(decodeUtf8(await boundedResponse(upstream)));
    if (!isSafeSuccess(route, value)) return unavailable();
    return safe(value, upstream.status);
  } catch { return unavailable(); }
}

function errorForRoute(route: CentralAdminRoute): string { return route === "owner-transfer" ? "invalid_card_transfer" : "invalid_card_revoke"; }

async function validate(route: CentralAdminRoute, request: Request, publicOrigin: string, cardId?: string): Promise<"valid" | "forbidden" | "invalid"> {
  try {
    const descriptor = ROUTES[route];
    const url = new URL(request.url);
    if (request.method !== descriptor.method || !hasPath(route, url.pathname, cardId) || !expectedHost(request.headers, publicOrigin) || !hasSessionCookie(request.headers) || hasCallerClaim(request.headers)) return "forbidden";
    if (route === "org" && url.search) return "forbidden";
    if (route === "scorecard" && !validScorecardQuery(url.searchParams)) return "forbidden";
    if (descriptor.method === "GET") return await emptyBody(request) && request.headers.get("content-type") === null ? "valid" : "forbidden";
    if (!postEnvelope(request.headers, publicOrigin)) return "forbidden";
    await readCentralAdminBody(route, request.clone());
    return "valid";
  } catch { return "invalid"; }
}

function hasPath(route: CentralAdminRoute, pathname: string, cardId?: string): boolean {
  const descriptor = ROUTES[route];
  if (descriptor.needsCard && !isReference(cardId)) return false;
  return pathname === descriptor.publicPrefix.replace("{card_id}", cardId ?? "");
}

function validScorecardQuery(params: URLSearchParams): boolean {
  const keys = Array.from(params.keys());
  if (keys.some((key) => key !== "since" && key !== "until") || keys.filter((key) => key === "since").length > 1 || keys.filter((key) => key === "until").length > 1) return false;
  const since = params.get("since"); const until = params.get("until");
  if ((since === null) !== (until === null)) return false;
  return (since === null && until === null) || (validTimestamp(since) && validTimestamp(until));
}

function validTimestamp(value: string | null): boolean {
  if (value === null || !RFC3339_UTC.test(value)) return false;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed);
}

function validDto(route: CentralAdminRoute, value: Json): boolean {
  if (route === "owner-transfer") return exact(value, ["new_owner_user_id", "expected_card_revision", "expected_assignment_generation", "expected_assignment_revision"]) && isReference(value.new_owner_user_id) && positive(value.expected_card_revision) && positive(value.expected_assignment_generation) && positive(value.expected_assignment_revision);
  if (route === "revoke") return exact(value, ["reason_code", "expected_card_revision", "expected_assignment_generation", "expected_assignment_revision"]) && typeof value.reason_code === "string" && REASON_CODE.test(value.reason_code) && positive(value.expected_card_revision) && positive(value.expected_assignment_generation) && positive(value.expected_assignment_revision);
  return false;
}

function isSafeSuccess(route: CentralAdminRoute, value: unknown): value is Json {
  if (!isObject(value)) return false;
  if (route === "org") return exact(value, ["registry_revision", "policy_epoch", "policy_digest", "nodes", "edges", "source_digest"]) && positive(value.registry_revision) && positive(value.policy_epoch) && sha(value.policy_digest) && sha(value.source_digest) && Array.isArray(value.nodes) && value.nodes.every(validNode) && Array.isArray(value.edges) && value.edges.every(validEdge);
  if (route === "scorecard") return exact(value, ["window", "source_digest", "owners"]) && isObject(value.window) && exact(value.window, ["since", "until"]) && validTimestamp(value.window.since as string) && validTimestamp(value.window.until as string) && sha(value.source_digest) && Array.isArray(value.owners) && value.owners.every(validOwnerScorecard);
  if (route === "owner-transfer") return exact(value, ["receipt_id", "card_id", "card_revision", "registry_revision", "closed_assignment_id", "new_assignment_id", "new_generation", "new_assignment_revision", "from_owner_user_id", "to_owner_user_id", "policy_epoch", "policy_digest", "replayed"]) && refs(value, ["receipt_id", "card_id", "closed_assignment_id", "new_assignment_id", "from_owner_user_id", "to_owner_user_id"]) && positive(value.card_revision) && positive(value.registry_revision) && positive(value.new_generation) && positive(value.new_assignment_revision) && positive(value.policy_epoch) && sha(value.policy_digest) && typeof value.replayed === "boolean";
  return exact(value, ["receipt_id", "card_id", "card_revision", "registry_revision", "closed_assignment_id", "closed_generation", "closed_assignment_revision", "recorded_owner_user_id", "reason_code", "policy_epoch", "policy_digest", "replayed"]) && refs(value, ["receipt_id", "card_id", "closed_assignment_id", "recorded_owner_user_id"]) && positive(value.card_revision) && positive(value.registry_revision) && positive(value.closed_generation) && positive(value.closed_assignment_revision) && REASON_CODE.test(String(value.reason_code)) && positive(value.policy_epoch) && sha(value.policy_digest) && typeof value.replayed === "boolean";
}

function validNode(value: unknown): boolean {
  if (!isObject(value)) return false;
  if (value.kind === "user") return exact(value, ["kind", "user_id", "manager_user_id"]) && isReference(value.user_id) && (value.manager_user_id === null || isReference(value.manager_user_id));
  return exact(value, ["kind", "card_id", "card_revision", "team", "assignment_status", "assignment_generation", "current_owner_user_id", "recorded_owner_user_id"]) && value.kind === "agent_card" && isReference(value.card_id) && positive(value.card_revision) && typeof value.team === "string" && (value.assignment_status === "active" || value.assignment_status === "revoked" || value.assignment_status === "none") && (value.assignment_generation === null || positive(value.assignment_generation)) && (value.current_owner_user_id === null || isReference(value.current_owner_user_id)) && isReference(value.recorded_owner_user_id);
}

function validEdge(value: unknown): boolean { return isObject(value) && exact(value, ["kind", "source_id", "target_id"]) && (value.kind === "owns" || value.kind === "manages" || value.kind === "maintains") && isReference(value.source_id) && isReference(value.target_id); }
function validOwnerScorecard(value: unknown): boolean { return isObject(value) && exact(value, ["owner_user_id", "quality", "supervision", "availability", "freshness", "weak_identity_note"]) && isReference(value.owner_user_id) && axis(value.quality) && axis(value.supervision) && axis(value.availability) && axis(value.freshness) && (value.weak_identity_note === null || typeof value.weak_identity_note === "string"); }
function axis(value: unknown): boolean { return isObject(value) && Object.values(value).every((item) => item === null || typeof item === "number" || typeof item === "string" || typeof item === "boolean"); }
function refs(value: Json, keys: readonly string[]): boolean { return keys.every((key) => isReference(value[key])); }
function positive(value: unknown): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value > 0; }
function sha(value: unknown): value is string { return typeof value === "string" && SHA256.test(value); }
function isReference(value: unknown): value is string { return typeof value === "string" && REFERENCE.test(value); }
function exact(value: Json, keys: readonly string[]): boolean { return Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key)); }
function isObject(value: unknown): value is Json { return typeof value === "object" && value !== null && !Array.isArray(value); }
function postEnvelope(headers: Headers, origin: string): boolean { return headers.get("origin") === origin && headers.get("sec-fetch-site") === "same-origin" && headers.get("sec-fetch-mode") === "cors" && headers.get("sec-fetch-dest") === "empty" && CSRF.test(headers.get("x-aon-csrf") ?? "") && IDEMPOTENCY_KEY.test(headers.get("idempotency-key") ?? "") && /^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(headers.get("content-type") ?? ""); }
function hasCallerClaim(headers: Headers): boolean {
  const provenance = headers.get(PROVENANCE);
  if (provenance !== null && provenance !== CLEAN_PROVENANCE) return true;
  const trusted = provenance === CLEAN_PROVENANCE && Reflect.get(globalThis, PROOF_SYMBOL) === headers.get(PROOF_HEADER);
  for (const original of Array.from(headers.keys())) {
    const name = original.toLowerCase();
    if (name === PROVENANCE || name === PROOF_HEADER) continue;
    if (SELF_CLAIMS.has(name)) { if (trusted && FORWARDED.has(name)) continue; return true; }
    if (name.startsWith("x-aon-") && name !== "x-aon-csrf") return true;
    if (name.startsWith("x-forwarded-") && !(trusted && FORWARDED.has(name))) return true;
  }
  return false;
}
function expectedHost(headers: Headers, publicOrigin: string): boolean { const host = headers.get("host"); if (host === null || /[\s,]/.test(host)) return false; try { if (new URL(`http://${host}`).host === new URL(publicOrigin).host) return true; return process.env.HOSTNAME !== undefined && process.env.PORT !== undefined && host === `${process.env.HOSTNAME}:${process.env.PORT}`; } catch { return false; } }
function hasSessionCookie(headers: Headers): boolean { return /(?:^|;\s*)__Host-aon-central-session=[A-Za-z0-9_-]+(?:;|$)/.test(headers.get("cookie") ?? ""); }
function decodeUtf8(value: Uint8Array): string { return new TextDecoder("utf-8", { fatal: true }).decode(value); }
function parseJson(value: string): unknown { return JSON.parse(value); }
function safe(value: Json, status: number): Response { return new Response(JSON.stringify(value), { status, headers: { "cache-control": "no-store", "content-type": "application/json; charset=utf-8" } }); }
function unavailable(): Response { return safe({ code: "unavailable", message: MESSAGE.unavailable }, 502); }
async function relayError(upstream: Response): Promise<Response> { try { const value = parseJson(decodeUtf8(await boundedResponse(upstream))); if (!isObject(value) || !exact(value, ["error"]) || typeof value.error !== "string" || !SAFE_ERRORS[upstream.status].has(value.error)) return unavailable(); return safe({ code: value.error, message: MESSAGE[value.error] ?? MESSAGE.unavailable }, upstream.status); } catch { return unavailable(); } }
async function emptyBody(request: Request): Promise<boolean> { const length = request.headers.get("content-length"); if (length !== null && length !== "0") return false; if (request.body === null) return true; const reader = request.body.getReader(); try { while (true) { const next = await reader.read(); if (next.done) return true; if (next.value.byteLength > 0) return false; } } finally { reader.releaseLock(); } }
async function boundedRequest(request: Request, maximum: number): Promise<Uint8Array> { const declared = request.headers.get("content-length"); if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > maximum)) throw new RangeError("admin body too large"); if (request.body === null) return new Uint8Array(); return boundedStream(request.body, maximum); }
async function boundedResponse(response: Response): Promise<Uint8Array> { const declared = response.headers.get("content-length"); if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_RESPONSE)) throw new RangeError("admin response too large"); if (response.body === null) return new Uint8Array(); return boundedStream(response.body, MAX_RESPONSE); }
async function boundedStream(body: ReadableStream<Uint8Array>, maximum: number): Promise<Uint8Array> { const reader = body.getReader(); const chunks: Uint8Array[] = []; let length = 0; try { while (true) { const next = await reader.read(); if (next.done) break; length += next.value.byteLength; if (length > maximum) throw new RangeError("admin payload too large"); chunks.push(next.value); } } finally { reader.releaseLock(); } const result = new Uint8Array(length); let offset = 0; for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.byteLength; } return result; }
