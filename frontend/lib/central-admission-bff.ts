/** Fixed, browser-session admission relay. It is intentionally not the generic Question BFF. */
export type AdmissionRoute = "onboarding-status" | "users-get" | "users-post" | "cards-get" | "cards-post";

const CENTRAL_ADMISSION_UPSTREAM = "http://127.0.0.1:8010";
const UPSTREAM: Record<AdmissionRoute, { method: "GET" | "POST"; path: string }> = {
  "onboarding-status": { method: "GET", path: "/onboarding/status" },
  "users-get": { method: "GET", path: "/admin/users" },
  "users-post": { method: "POST", path: "/admin/users" },
  "cards-get": { method: "GET", path: "/admin/agent-cards" },
  "cards-post": { method: "POST", path: "/admin/agent-cards" },
};
const MAX_BODY_BYTES = 65_536;
const MAX_RESPONSE_BYTES = 1_048_576;
const SELF_CLAIMS = new Set(["authorization", "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-user", "x-org", "x-role", "x-permission", "x-token", "x-token-claim", "x-aon-user", "x-aon-org", "x-aon-role", "x-aon-permission", "x-aon-token", "x-aon-token-claim", "x-aon-session", "x-aon-actor", "x-aon-authority"]);
const SYNTHETIC_FORWARDED = new Set(["x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"]);
const PROVENANCE_HEADER = "x-aon-admission-proxy-provenance";
const TRUSTED_NEXT_PROVENANCE = "next-standalone-clean";
const POST_HEADERS = ["cookie", "origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "x-aon-csrf", "idempotency-key", "content-type"] as const;

export function admissionUpstreamUrl(route: AdmissionRoute): URL {
  return new URL(`${CENTRAL_ADMISSION_UPSTREAM}${UPSTREAM[route].path}`);
}

export async function isValidAdmissionRequest(route: AdmissionRoute, request: Request, publicOrigin: string): Promise<boolean> {
  if (request.method !== UPSTREAM[route].method || new URL(request.url).search || !hasExpectedAdmissionHost(request.headers, publicOrigin) || hasSelfClaim(request.headers)) return false;
  if (request.method === "GET") return hasEmptyBody(request);
  if (request.headers.get("origin") !== publicOrigin || request.headers.get("sec-fetch-site") !== "same-origin" || request.headers.get("sec-fetch-mode") !== "cors" || request.headers.get("sec-fetch-dest") !== "empty") return false;
  if (!isOpaque(request.headers.get("x-aon-csrf"), 32, 128) || !isOpaque(request.headers.get("idempotency-key"), 1, 128)) return false;
  const contentType = request.headers.get("content-type");
  if (contentType === null || !/^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(contentType)) return false;
  try { await readAdmissionBody(request.clone()); return true; } catch { return false; }
}

export function admissionRequestHeaders(route: AdmissionRoute, source: Headers): Headers {
  const result = new Headers();
  const names = UPSTREAM[route].method === "GET" ? ["cookie"] : POST_HEADERS;
  for (const name of names) { const value = source.get(name); if (value !== null) result.set(name, value); }
  return result;
}

export async function readAdmissionBody(request: Request): Promise<Uint8Array> {
  const declared = request.headers.get("content-length");
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_BODY_BYTES)) throw new RangeError("admission body too large");
  if (request.body === null) return new Uint8Array();
  const reader = request.body.getReader(); const chunks: Uint8Array[] = []; let size = 0;
  try { while (true) { const next = await reader.read(); if (next.done) break; size += next.value.byteLength; if (size > MAX_BODY_BYTES) throw new RangeError("admission body too large"); chunks.push(next.value); } } finally { reader.releaseLock(); }
  const result = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.byteLength; }
  return result;
}

export async function relayAdmissionResponse(upstream: Response): Promise<Response> {
  try {
    const body = await readBounded(upstream, MAX_RESPONSE_BYTES);
    const headers = new Headers({ "cache-control": "no-store" });
    const type = upstream.headers.get("content-type"); if (type !== null) headers.set("content-type", type);
    return new Response(upstream.status === 204 || upstream.status === 205 || upstream.status === 304 ? null : body, { status: upstream.status, headers });
  } catch { return admissionUnavailable(); }
}

export function admissionUnavailable(): Response {
  return new Response(JSON.stringify({ error: "central_admission_unavailable" }), { status: 502, headers: { "cache-control": "no-store", "content-type": "application/json" } });
}

function hasSelfClaim(headers: Headers): boolean {
  const provenance = headers.get(PROVENANCE_HEADER);
  if (provenance !== null && provenance !== TRUSTED_NEXT_PROVENANCE) return true;
  const trustedSyntheticForwarding = provenance === TRUSTED_NEXT_PROVENANCE;
  for (const original of Array.from(headers.keys())) {
    const name = original.toLowerCase();
    if (name === PROVENANCE_HEADER) continue;
    if (!SELF_CLAIMS.has(name)) continue;
    // These are admitted only after the standalone entrypoint has examined
    // raw HTTP headers, removed caller supplied values and marked the request.
    // Equality with URL/loopback values is not provenance.
    if (trustedSyntheticForwarding && SYNTHETIC_FORWARDED.has(name)) continue;
    return true;
  }
  return false;
}
function hasExpectedAdmissionHost(headers: Headers, publicOrigin: string): boolean {
  const rawHost = headers.get("host");
  if (rawHost === null || /[\s,]/.test(rawHost)) return false;
  try {
    const publicHost = new URL(publicOrigin).host;
    if (new URL(`http://${rawHost}`).host === publicHost) return true;
    const bindHost = process.env.HOSTNAME;
    const bindPort = process.env.PORT;
    return bindHost !== undefined && bindPort !== undefined && rawHost === `${bindHost}:${bindPort}`;
  } catch { return false; }
}
function isOpaque(value: string | null, min: number, max: number): boolean { return value !== null && new TextEncoder().encode(value).byteLength >= min && new TextEncoder().encode(value).byteLength <= max && /^[A-Za-z0-9_-]+$/.test(value); }
async function hasEmptyBody(request: Request): Promise<boolean> {
  const declared = request.headers.get("content-length"); if (declared !== null && declared !== "0") return false;
  if (request.body === null) return true;
  const reader = request.body.getReader();
  try { while (true) { const next = await reader.read(); if (next.done) return true; if (next.value.byteLength !== 0) return false; } } catch { return false; } finally { reader.releaseLock(); }
}
async function readBounded(response: Response, maximum: number): Promise<ArrayBuffer> {
  const declared = response.headers.get("content-length"); if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > maximum)) throw new RangeError("upstream body too large");
  if (response.body === null) return new ArrayBuffer(0); const reader = response.body.getReader(); const chunks: Uint8Array[] = []; let size = 0;
  try { while (true) { const next = await reader.read(); if (next.done) break; size += next.value.byteLength; if (size > maximum) throw new RangeError("upstream body too large"); chunks.push(next.value); } } finally { reader.releaseLock(); }
  const result = new Uint8Array(size); let offset = 0; for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.byteLength; } return result.buffer;
}
