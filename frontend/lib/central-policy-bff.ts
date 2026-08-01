/** Exact Central PolicyRevision BFF: no generic proxy or caller identity headers. */
export type PolicyRoute = "policy-get" | "policy-post";

const UPSTREAM: Record<PolicyRoute, { method: "GET" | "POST"; path: string }> = {
  "policy-get": { method: "GET", path: "/v1/admin/policy" },
  "policy-post": { method: "POST", path: "/v1/admin/policy/revisions" },
};
const ORIGIN = "http://127.0.0.1:8010";
const MAX_BODY = 65_536;
const MAX_RESPONSE = 1_048_576;
const SELF_CLAIMS = new Set(["authorization", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-aon-user", "x-aon-org", "x-aon-role", "x-aon-permission", "x-aon-token-claim", "x-aon-session", "x-aon-actor", "x-aon-authority"]);

export function policyUpstreamUrl(route: PolicyRoute): URL { return new URL(`${ORIGIN}${UPSTREAM[route].path}`); }

export async function isValidPolicyRequest(route: PolicyRoute, request: Request, publicOrigin: string): Promise<boolean> {
  const descriptor = UPSTREAM[route];
  if (request.method !== descriptor.method || new URL(request.url).search || request.headers.get("host") === null || hasSelfClaim(request.headers)) return false;
  if (route === "policy-get") return await hasEmptyBody(request) && request.headers.get("content-type") === null;
  if (request.headers.get("origin") !== publicOrigin || request.headers.get("sec-fetch-site") !== "same-origin" || request.headers.get("sec-fetch-mode") !== "cors" || request.headers.get("sec-fetch-dest") !== "empty") return false;
  if (!opaque(request.headers.get("x-aon-csrf"), 32, 128) || !opaque(request.headers.get("idempotency-key"), 1, 128)) return false;
  const type = request.headers.get("content-type");
  if (type === null || !/^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(type)) return false;
  try { await readBody(request.clone()); return true; } catch { return false; }
}

export function policyRequestHeaders(route: PolicyRoute, source: Headers): Headers {
  const result = new Headers();
  const names = route === "policy-get" ? ["cookie"] : ["cookie", "origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "x-aon-csrf", "idempotency-key", "content-type"];
  for (const name of names) { const value = source.get(name); if (value !== null) result.set(name, value); }
  return result;
}

export async function readPolicyBody(request: Request): Promise<Uint8Array> { return readBody(request); }

export async function relayPolicyResponse(response: Response): Promise<Response> {
  try {
    const body = await readBounded(response, MAX_RESPONSE);
    const headers = new Headers({ "cache-control": "no-store" });
    const type = response.headers.get("content-type"); if (type !== null) headers.set("content-type", type);
    return new Response(body, { status: response.status, headers });
  } catch { return new Response(JSON.stringify({ error: "central_policy_unavailable" }), { status: 502, headers: { "cache-control": "no-store", "content-type": "application/json" } }); }
}

function hasSelfClaim(headers: Headers): boolean { for (const name of Array.from(headers.keys())) if (SELF_CLAIMS.has(name.toLowerCase())) return true; return false; }
function opaque(value: string | null, min: number, max: number): boolean { return value !== null && value.length >= min && value.length <= max && /^[A-Za-z0-9._:-]+$/.test(value); }
async function hasEmptyBody(request: Request): Promise<boolean> { const length = request.headers.get("content-length"); if (length !== null && length !== "0") return false; if (request.body === null) return true; const reader = request.body.getReader(); try { while (true) { const next = await reader.read(); if (next.done) return true; if (next.value.byteLength > 0) return false; } } finally { reader.releaseLock(); } }
async function readBody(request: Request): Promise<Uint8Array> { const declared = request.headers.get("content-length"); if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_BODY)) throw new RangeError("body too large"); if (request.body === null) return new Uint8Array(); const reader = request.body.getReader(); const chunks: Uint8Array[] = []; let size = 0; try { while (true) { const next = await reader.read(); if (next.done) break; size += next.value.byteLength; if (size > MAX_BODY) throw new RangeError("body too large"); chunks.push(next.value); } } finally { reader.releaseLock(); } const body = new Uint8Array(size); let offset = 0; for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; } return body; }
async function readBounded(response: Response, maximum: number): Promise<ArrayBuffer> { const declared = response.headers.get("content-length"); if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > maximum)) throw new RangeError("response too large"); if (response.body === null) return new ArrayBuffer(0); const reader = response.body.getReader(); const chunks: Uint8Array[] = []; let size = 0; try { while (true) { const next = await reader.read(); if (next.done) break; size += next.value.byteLength; if (size > maximum) throw new RangeError("response too large"); chunks.push(next.value); } } finally { reader.releaseLock(); } const body = new Uint8Array(size); let offset = 0; for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; } return body.buffer; }
