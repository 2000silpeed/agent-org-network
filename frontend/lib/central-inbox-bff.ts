/** Dedicated exact Central inbox browser relay (ADR 0080). */

export type CentralInboxRoute =
  | "conflict-list" | "conflict-detail" | "conflict-concurrence"
  | "backup-list" | "backup-detail" | "backup-disposition"
  | "reevaluation-list" | "reevaluation-detail" | "reevaluation-disposition"
  | "approval-list" | "approval-detail" | "approval-disposition" | "approval-reassignment";

type Descriptor = {
  method: "GET" | "POST";
  collection: "conflicts" | "backup-reviews" | "reevaluations" | "approvals";
  suffix?: "concurrences" | "dispositions" | "reassignments";
};
type FetchLike = (input: URL, init: RequestInit) => Promise<Response>;
type Json = Record<string, unknown>;

const CENTRAL = "http://127.0.0.1:8010";
const MAX_BODY = 65_536;
const MAX_RESPONSE = 1_048_576;
const TIMEOUT_MS = 10_000;
const REFERENCE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const IDEMPOTENCY_KEY = REFERENCE;
const PROVENANCE = "x-aon-admission-proxy-provenance";
const PROOF_HEADER = "aon-standalone-provenance-proof";
const PROOF_SYMBOL = Symbol.for("agent-org-network.standalone-provenance-proof");
const CLEAN_PROVENANCE = "next-standalone-clean";
const FORWARDED = new Set([
  "x-forwarded-for", "x-forwarded-host", "x-forwarded-port", "x-forwarded-proto",
]);
const SELF_CLAIMS = new Set([
  "authorization", "forwarded", "x-user", "x-org", "x-owner", "x-role", "x-permission",
  "x-token", "x-token-claim", "x-aon-user", "x-aon-org", "x-aon-owner", "x-aon-role",
  "x-aon-permission", "x-aon-token", "x-aon-token-claim", "x-aon-session", "x-aon-actor",
  "x-aon-authority", "x-aon-internal",
]);
const ROUTES: Record<CentralInboxRoute, Descriptor> = {
  "conflict-list": { method: "GET", collection: "conflicts" },
  "conflict-detail": { method: "GET", collection: "conflicts" },
  "conflict-concurrence": { method: "POST", collection: "conflicts", suffix: "concurrences" },
  "backup-list": { method: "GET", collection: "backup-reviews" },
  "backup-detail": { method: "GET", collection: "backup-reviews" },
  "backup-disposition": { method: "POST", collection: "backup-reviews", suffix: "dispositions" },
  "reevaluation-list": { method: "GET", collection: "reevaluations" },
  "reevaluation-detail": { method: "GET", collection: "reevaluations" },
  "reevaluation-disposition": { method: "POST", collection: "reevaluations", suffix: "dispositions" },
  "approval-list": { method: "GET", collection: "approvals" },
  "approval-detail": { method: "GET", collection: "approvals" },
  "approval-disposition": { method: "POST", collection: "approvals", suffix: "dispositions" },
  "approval-reassignment": { method: "POST", collection: "approvals", suffix: "reassignments" },
};
const ERRORS: Record<number, ReadonlySet<string>> = {
  401: new Set(["session_unavailable"]),
  404: new Set(["not_found_or_denied"]),
  409: new Set(["stale_or_conflict"]),
  422: new Set(["invalid_input"]),
  503: new Set(["unavailable"]),
};
const MESSAGES: Record<string, string> = {
  session_unavailable: "로그인이 필요하거나 세션이 만료되었습니다.",
  not_found_or_denied: "처리함 항목을 찾을 수 없습니다.",
  stale_or_conflict: "항목이 변경되었습니다. 새로 고친 뒤 다시 시도해 주세요.",
  invalid_input: "처리함 요청 형식이 올바르지 않습니다.",
  unavailable: "처리함 서비스를 지금 사용할 수 없습니다.",
};

export function inboxUpstreamUrl(route: CentralInboxRoute, aggregateId?: string): URL {
  const descriptor = ROUTES[route];
  if (needsId(route) !== isReference(aggregateId)) throw new RangeError("invalid inbox aggregate id");
  const path = `/v1/inbox/${descriptor.collection}${aggregateId ? `/${aggregateId}` : ""}${descriptor.suffix ? `/${descriptor.suffix}` : ""}`;
  return new URL(`${CENTRAL}${path}`);
}

export async function isValidCentralInboxRequest(
  route: CentralInboxRoute, request: Request, publicOrigin: string, aggregateId?: string,
): Promise<boolean> {
  return await validate(route, request, publicOrigin, aggregateId) === "valid";
}

type Validity = "valid" | "forbidden" | "invalid";

async function validate(
  route: CentralInboxRoute, request: Request, publicOrigin: string, aggregateId?: string,
): Promise<Validity> {
  try {
    const descriptor = ROUTES[route];
    const url = new URL(request.url);
    if (request.method !== descriptor.method || url.search || !expectedPath(route, url.pathname, aggregateId)) return "forbidden";
    if (!expectedHost(request.headers, publicOrigin) || !hasSessionCookie(request.headers) || hasCallerClaim(request.headers)) return "forbidden";
    if (descriptor.method === "GET") return await emptyBody(request) ? "valid" : "forbidden";
    if (!postEnvelope(request.headers, publicOrigin)) return "forbidden";
    await readCentralInboxBody(route, request.clone());
    return "valid";
  } catch {
    return "invalid";
  }
}

export async function readCentralInboxBody(
  route: CentralInboxRoute, request: Request,
): Promise<Uint8Array> {
  if (ROUTES[route].method !== "POST") throw new TypeError("inbox route has no body");
  const bytes = await bounded(request, MAX_BODY);
  const body = parse(bytes);
  if (!isObject(body) || !validWrite(route, body)) throw new TypeError("invalid inbox dto");
  return bytes;
}

export async function handleCentralInboxBff(
  route: CentralInboxRoute,
  request: Request,
  publicOrigin: string,
  aggregateId?: string,
  fetcher: FetchLike = fetch,
): Promise<Response> {
  const validity = await validate(route, request, publicOrigin, aggregateId);
  if (validity === "forbidden") return safe({ code: "invalid_input", message: MESSAGES.invalid_input }, 403);
  if (validity === "invalid") return safe({ code: "invalid_input", message: MESSAGES.invalid_input }, 422);
  let body: Uint8Array | undefined;
  try {
    body = ROUTES[route].method === "POST" ? await readCentralInboxBody(route, request) : undefined;
  } catch {
    return safe({ code: "invalid_input", message: MESSAGES.invalid_input }, 422);
  }
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (request.signal.aborted) controller.abort();
  else request.signal.addEventListener("abort", abort, { once: true });
  const timeout = setTimeout(abort, TIMEOUT_MS);
  try {
    const upstream = await fetcher(inboxUpstreamUrl(route, aggregateId), {
      method: ROUTES[route].method,
      headers: requestHeaders(route, request.headers),
      body: body as unknown as BodyInit | undefined,
      redirect: "manual",
      cache: "no-store",
      signal: controller.signal,
    });
    return await relayCentralInboxResponse(route, upstream);
  } catch {
    return unavailable();
  } finally {
    clearTimeout(timeout);
    request.signal.removeEventListener("abort", abort);
  }
}

export async function relayCentralInboxResponse(
  route: CentralInboxRoute, upstream: Response,
): Promise<Response> {
  if (ERRORS[upstream.status] !== undefined) {
    try {
      const value = parse(await boundedResponse(upstream));
      if (!isObject(value) || !exact(value, ["error"]) || typeof value.error !== "string" || !ERRORS[upstream.status].has(value.error)) return unavailable();
      return safe({ code: value.error, message: MESSAGES[value.error] }, upstream.status);
    } catch {
      return unavailable();
    }
  }
  if (upstream.status !== 200) return unavailable();
  try {
    const value = parse(await boundedResponse(upstream));
    if (!isSafeSuccess(route, value)) return unavailable();
    return safe(value, 200);
  } catch {
    return unavailable();
  }
}

function requestHeaders(route: CentralInboxRoute, source: Headers): Headers {
  const result = new Headers();
  const names = ROUTES[route].method === "GET"
    ? ["cookie"]
    : ["cookie", "origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "x-aon-csrf", "idempotency-key", "content-type"];
  for (const name of names) {
    const value = source.get(name);
    if (value !== null) result.set(name, value);
  }
  return result;
}

function validWrite(route: CentralInboxRoute, value: Json): boolean {
  if (route === "conflict-concurrence") return exact(value, [
    "on_candidate_card_id", "stance", "rationale", "expected_case_revision", "expected_request_revision", "expected_round",
  ]) && isReference(value.on_candidate_card_id) && (value.stance === "keep_as_complement" || value.stance === "withdraw") &&
    text(value.rationale, false) && positive(value.expected_case_revision) && nonnegative(value.expected_request_revision) && positive(value.expected_round);
  if (route === "backup-disposition") {
    const keys = value.kind === "correct" ? ["kind", "corrected_text", "rationale", "expected_revision"] : ["kind", "rationale", "expected_revision"];
    return exact(value, keys) && (value.kind === "approve" || value.kind === "dismiss" || value.kind === "correct") &&
      text(value.rationale, false) && positive(value.expected_revision) && (value.kind !== "correct" || text(value.corrected_text, false));
  }
  if (route === "reevaluation-disposition") return exact(value, ["kind", "rationale", "expected_revision"]) &&
    (value.kind === "acknowledge" || value.kind === "request_reanswer") && text(value.rationale, false) && positive(value.expected_revision);
  if (route === "approval-disposition") {
    const common = ["kind", "expected_approval_item_revision", "expected_request_revision"];
    const keys = value.kind === "approve_with_edit" ? [...common, "edited_text"] : value.kind === "reject" ? [...common, "reason_code"] : common;
    return exact(value, keys) && (value.kind === "approve" || value.kind === "approve_with_edit" || value.kind === "reject") &&
      positive(value.expected_approval_item_revision) && nonnegative(value.expected_request_revision) &&
      (value.kind !== "approve_with_edit" || text(value.edited_text, false)) && (value.kind !== "reject" || text(value.reason_code, false));
  }
  if (route === "approval-reassignment") return exact(value, [
    "target_approver_user_id", "target_approval_card_id", "expected_approval_item_revision", "expected_request_revision",
  ]) && isReference(value.target_approver_user_id) && isReference(value.target_approval_card_id) &&
    positive(value.expected_approval_item_revision) && nonnegative(value.expected_request_revision);
  return false;
}

function isSafeSuccess(route: CentralInboxRoute, value: unknown): value is Json {
  if (!isObject(value)) return false;
  if (route.endsWith("-list")) {
    if (!exact(value, ["items"]) || !Array.isArray(value.items)) return false;
    const itemRoute = route.replace("-list", "-detail") as CentralInboxRoute;
    return value.items.every((item) => isSummary(itemRoute, item));
  }
  if (route.endsWith("-detail")) return isDetail(route, value);
  if (route === "conflict-concurrence") return exact(value, [
    "receipt_id", "concurrence_command_digest", "case_id", "case_revision", "request_id", "request_revision", "state", "outcome", "replayed",
  ]) && refs(value, ["receipt_id", "case_id", "request_id"]) && sha(value.concurrence_command_digest) &&
    positive(value.case_revision) && nonnegative(value.request_revision) && (value.state === "open" || value.state === "resolved") &&
    ["still_open", "agreed", "deadlocked", "route_rejected"].includes(String(value.outcome)) && typeof value.replayed === "boolean";
  if (route === "backup-disposition") return action(value, "review_id", ["correction_record_id"]);
  if (route === "reevaluation-disposition") return action(value, "reevaluation_id", ["reanswer_requested_id"]);
  if (route === "approval-disposition") return exact(value, [
    "receipt_id", "approval_item_id", "approval_item_revision", "request_id", "request_revision", "state", "replayed",
  ]) && refs(value, ["receipt_id", "approval_item_id", "request_id"]) && positive(value.approval_item_revision) &&
    nonnegative(value.request_revision) && (value.state === "approved" || value.state === "rejected") && typeof value.replayed === "boolean";
  return exact(value, [
    "receipt_id", "superseded_approval_item_id", "successor_approval_item_id", "successor_approval_item_revision",
    "request_id", "request_revision", "state", "replayed",
  ]) && refs(value, ["receipt_id", "superseded_approval_item_id", "successor_approval_item_id", "request_id"]) &&
    positive(value.successor_approval_item_revision) && nonnegative(value.request_revision) && value.state === "open" && typeof value.replayed === "boolean";
}

function isSummary(route: CentralInboxRoute, value: unknown): boolean {
  if (!isObject(value)) return false;
  if (route === "conflict-detail") return exact(value, ["case_id", "request_id", "request_revision", "state", "round", "revision", "candidate_card_ids", "opened_at"]) &&
    refs(value, ["case_id", "request_id"]) && nonnegative(value.request_revision) && positive(value.round) && positive(value.revision) &&
    ["open", "resolved", "escalated"].includes(String(value.state)) && refArray(value.candidate_card_ids) && timestamp(value.opened_at);
  if (route === "backup-detail") return exact(value, ["review_id", "request_id", "source_answer_record_id", "revision", "state", "created_at"]) &&
    refs(value, ["review_id", "request_id", "source_answer_record_id"]) && positive(value.revision) && (value.state === "open" || value.state === "reviewed") && timestamp(value.created_at);
  if (route === "reevaluation-detail") return exact(value, ["reevaluation_id", "request_id", "feedback_id", "source_answer_record_id", "revision", "state", "created_at"]) &&
    refs(value, ["reevaluation_id", "request_id", "feedback_id", "source_answer_record_id"]) && positive(value.revision) &&
    (value.state === "open" || value.state === "reviewed") && timestamp(value.created_at);
  return exact(value, ["approval_item_id", "request_id", "request_revision", "approval_round", "revision", "assigned_at", "due_at", "state"]) &&
    refs(value, ["approval_item_id", "request_id"]) && positive(value.request_revision) &&
    positive(value.approval_round) && positive(value.revision) && timestamp(value.assigned_at) &&
    timestamp(value.due_at) && value.state === "open";
}

function isDetail(route: CentralInboxRoute, value: Json): boolean {
  if (route === "conflict-detail") {
    const summary = ["case_id", "request_id", "request_revision", "state", "round", "revision", "candidate_card_ids", "opened_at"];
    if (!exact(value, [...summary, "expected_case_revision", "expected_request_revision", "expected_round", "question", "candidates", "own_concurrence", "evidence_grants"])) return false;
    const base = Object.fromEntries(summary.map((key) => [key, value[key]]));
    return isSummary(route, base) && positive(value.expected_case_revision) && nonnegative(value.expected_request_revision) &&
      positive(value.expected_round) && text(value.question, true) && Array.isArray(value.candidates) &&
      value.candidates.every((item) => isObject(item) && exact(item, ["card_id", "card_revision", "card_digest", "owner_user_id", "concept_ref", "coverage_digest"]) &&
        refs(item, ["card_id", "owner_user_id", "concept_ref"]) && positive(item.card_revision) && sha(item.card_digest) && sha(item.coverage_digest)) &&
      (value.own_concurrence === null || (isObject(value.own_concurrence) &&
        exact(value.own_concurrence, ["on_candidate_card_id", "stance", "rationale", "round"]) &&
        isReference(value.own_concurrence.on_candidate_card_id) &&
        (value.own_concurrence.stance === "keep_as_complement" || value.own_concurrence.stance === "withdraw") &&
        text(value.own_concurrence.rationale, true) && positive(value.own_concurrence.round))) &&
      Array.isArray(value.evidence_grants) && value.evidence_grants.every((item) => isObject(item) &&
        exact(item, ["grant_id", "candidate_card_id", "candidate_card_revision", "concept_ref", "expires_at", "single_use", "status"]) &&
        refs(item, ["grant_id", "candidate_card_id", "concept_ref"]) && positive(item.candidate_card_revision) &&
        timestamp(item.expires_at) && typeof item.single_use === "boolean" &&
        ["available", "consumed", "expired"].includes(String(item.status)));
  }
  if (route === "backup-detail") return exact(value, [
    "review_id", "request_id", "source_answer_record_id", "revision", "state", "created_at", "question", "backup_answer_text",
    "answering_card_id", "answering_card_revision", "owner_user_id", "answered_at",
  ]) && refs(value, ["review_id", "request_id", "source_answer_record_id", "answering_card_id", "owner_user_id"]) &&
    positive(value.revision) && (value.state === "open" || value.state === "reviewed") &&
    text(value.question, true) && text(value.backup_answer_text, true) && positive(value.answering_card_revision) &&
    timestamp(value.created_at) && timestamp(value.answered_at);
  if (route === "reevaluation-detail") return exact(value, [
    "reevaluation_id", "request_id", "feedback_id", "source_answer_record_id", "revision", "state", "created_at", "question",
    "answer_text", "feedback_verdict", "feedback_comment", "answering_card_id", "answering_card_revision", "owner_user_id", "flagged_at",
  ]) && refs(value, ["reevaluation_id", "request_id", "feedback_id", "source_answer_record_id", "answering_card_id", "owner_user_id"]) &&
    positive(value.revision) && (value.state === "open" || value.state === "reviewed") &&
    value.feedback_verdict === "bad" && text(value.question, true) && text(value.answer_text, true) &&
    text(value.feedback_comment, true) && positive(value.answering_card_revision) && timestamp(value.created_at) && timestamp(value.flagged_at);
  return exact(value, [
    "approval_item_id", "request_id", "request_revision", "approval_round", "revision", "assigned_at", "due_at", "state", "question", "candidate_text",
    "candidate_digest", "policy_digest", "binding_version", "assigned_approver_user_id", "assigned_approval_card_id",
  ]) && refs(value, ["approval_item_id", "request_id", "assigned_approver_user_id", "assigned_approval_card_id"]) &&
    positive(value.request_revision) && positive(value.approval_round) && positive(value.revision) &&
    positive(value.binding_version) && timestamp(value.assigned_at) &&
    timestamp(value.due_at) && value.state === "open" && text(value.question, true) && text(value.candidate_text, true) &&
    sha(value.candidate_digest) && sha(value.policy_digest);
}

function action(value: Json, id: string, nullable: string[]): boolean {
  return exact(value, ["receipt_id", id, "revision", "state", ...nullable, "replayed"]) && refs(value, ["receipt_id", id]) &&
    positive(value.revision) && value.state === "reviewed" && nullable.every((key) => value[key] === null || isReference(value[key])) &&
    typeof value.replayed === "boolean";
}

function expectedPath(route: CentralInboxRoute, pathname: string, aggregateId?: string): boolean {
  try {
    const upstream = inboxUpstreamUrl(route, aggregateId);
    return pathname === upstream.pathname.replace("/v1/", "/api/");
  } catch { return false; }
}

function needsId(route: CentralInboxRoute): boolean { return !route.endsWith("-list"); }
function isReference(value: unknown): value is string { return typeof value === "string" && REFERENCE.test(value); }
function positive(value: unknown): value is number { return Number.isInteger(value) && Number(value) > 0; }
function nonnegative(value: unknown): value is number { return Number.isInteger(value) && Number(value) >= 0; }
function sha(value: unknown): boolean { return typeof value === "string" && /^[0-9a-f]{64}$/.test(value); }
function text(value: unknown, empty: boolean): boolean { return typeof value === "string" && (empty || value !== "") && !/[\uD800-\uDFFF]/.test(value); }
function timestamp(value: unknown): boolean { return typeof value === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$/.test(value); }
function refArray(value: unknown): boolean { return Array.isArray(value) && value.every(isReference); }
function refs(value: Json, keys: string[]): boolean { return keys.every((key) => isReference(value[key])); }
function isObject(value: unknown): value is Json { return typeof value === "object" && value !== null && !Array.isArray(value); }
function exact(value: Json, keys: string[]): boolean { return Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key)); }

function expectedHost(headers: Headers, publicOrigin: string): boolean {
  const host = headers.get("host");
  if (host === null || /[\s,]/.test(host)) return false;
  try {
    return new URL(`http://${host}`).host === new URL(publicOrigin).host ||
      (process.env.HOSTNAME !== undefined && process.env.PORT !== undefined && host === `${process.env.HOSTNAME}:${process.env.PORT}`);
  } catch { return false; }
}

function hasSessionCookie(headers: Headers): boolean {
  return (headers.get("cookie") ?? "").split(";").some((part) => part.trim().startsWith("__Host-aon-central-session="));
}

function hasCallerClaim(headers: Headers): boolean {
  const provenance = headers.get(PROVENANCE);
  const proof = headers.get(PROOF_HEADER);
  const processProof = Reflect.get(globalThis, PROOF_SYMBOL);
  const trusted = provenance === CLEAN_PROVENANCE && typeof processProof === "string" && proof === processProof;
  if (provenance !== null && !trusted) return true;
  if (proof !== null && !trusted) return true;
  for (const name of Array.from(headers.keys()).map((value) => value.toLowerCase())) {
    if (name === PROVENANCE || name === PROOF_HEADER) continue;
    if (SELF_CLAIMS.has(name)) return true;
    if (name.startsWith("x-aon-") && name !== "x-aon-csrf") return true;
    if (name.startsWith("x-forwarded-") && !(trusted && FORWARDED.has(name))) return true;
  }
  return false;
}

function postEnvelope(headers: Headers, origin: string): boolean {
  return headers.get("origin") === origin && headers.get("sec-fetch-site") === "same-origin" &&
    headers.get("sec-fetch-mode") === "cors" && headers.get("sec-fetch-dest") === "empty" &&
    typeof headers.get("x-aon-csrf") === "string" && (headers.get("x-aon-csrf") ?? "").length >= 32 &&
    IDEMPOTENCY_KEY.test(headers.get("idempotency-key") ?? "") &&
    /^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(headers.get("content-type") ?? "");
}

async function emptyBody(request: Request): Promise<boolean> {
  if (request.headers.get("content-length") !== null && request.headers.get("content-length") !== "0") return false;
  return (await request.arrayBuffer()).byteLength === 0;
}

async function bounded(request: Request, maximum: number): Promise<Uint8Array> {
  const declared = request.headers.get("content-length");
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > maximum)) throw new RangeError();
  const bytes = new Uint8Array(await request.arrayBuffer());
  if (bytes.byteLength < 2 || bytes.byteLength > maximum) throw new RangeError();
  return bytes;
}

async function boundedResponse(response: Response): Promise<Uint8Array> {
  if (!/^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(response.headers.get("content-type") ?? "")) throw new TypeError();
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.byteLength > MAX_RESPONSE) throw new RangeError();
  return bytes;
}

function parse(bytes: Uint8Array): unknown {
  const textValue = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  if (/[\uD800-\uDFFF]/.test(textValue)) throw new TypeError();
  return JSON.parse(textValue);
}

function safe(value: unknown, status: number): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "cache-control": "no-store", "content-type": "application/json; charset=utf-8" },
  });
}

function unavailable(): Response {
  return safe({ code: "unavailable", message: MESSAGES.unavailable }, 502);
}
