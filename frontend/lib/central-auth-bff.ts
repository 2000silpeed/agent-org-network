/**
 * The Central browser-auth BFF is deliberately separate from the Question
 * Request proxy.  It has four fixed paths and never accepts a caller-selected
 * upstream, identity claim, or redirect destination.
 */

export type CentralAuthRoute = "login-start" | "callback" | "session" | "logout";

const CENTRAL_AUTH_UPSTREAM = "http://127.0.0.1:8010";
const UPSTREAM_PATH: Record<CentralAuthRoute, string> = {
  "login-start": "/v1/browser-auth/login/start",
  callback: "/v1/browser-auth/callback",
  session: "/v1/browser-auth/session",
  logout: "/v1/browser-auth/logout",
};

const REQUEST_HEADERS = [
  "cookie", "origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "x-aon-csrf",
] as const;
const RESPONSE_HEADERS = ["location", "set-cookie", "cache-control", "content-type"] as const;
const MAX_AUTH_RESPONSE_BYTES = 1_048_576;
const SELF_CLAIM_HEADERS = new Set([
  "authorization", "x-user", "x-org", "x-role", "x-permission", "x-token", "x-token-claim",
  "x-aon-user", "x-aon-org", "x-aon-role", "x-aon-permission", "x-aon-token", "x-aon-token-claim",
]);

export function authUpstreamUrl(route: CentralAuthRoute, search: string): URL {
  const target = new URL(CENTRAL_AUTH_UPSTREAM);
  target.pathname = UPSTREAM_PATH[route];
  target.search = route === "callback" ? search : "";
  return target;
}

export async function isValidCentralAuthRequest(
  route: CentralAuthRoute,
  request: Request,
  publicOrigin: string,
): Promise<boolean> {
  if (!hasExpectedMethod(route, request.method) || hasSelfClaim(request.headers) || !await hasEmptyBody(request)) {
    return false;
  }
  if (route === "callback") return hasExactCallbackQuery(new URL(request.url));
  if (new URL(request.url).search) return false;
  if (route === "session") return true;
  return hasExpectedBrowserFetch(route, request.headers, publicOrigin);
}

export function centralAuthRequestHeaders(route: CentralAuthRoute, requestHeaders: Headers): Headers {
  const result = new Headers();
  for (const name of requestHeaderNames(route)) {
    const value = requestHeaders.get(name);
    if (value !== null) result.set(name, value);
  }
  return result;
}

export async function relayCentralAuthResponse(route: CentralAuthRoute, upstream: Response): Promise<Response> {
  if (upstream.status === 303 && (
    (route === "callback" && !hasSafeCallbackLocation(upstream.headers.get("location"))) ||
    (route === "login-start" && !hasSafeLoginStartLocation(upstream.headers.get("location")))
  )) {
    return new Response(null, { status: 502, headers: { "cache-control": "no-store" } });
  }
  const headers = centralAuthResponseHeaders(upstream.headers);
  const bodyless = upstream.status === 204 || upstream.status === 205 || upstream.status === 304;
  try {
    return new Response(bodyless ? null : await readBoundedAuthResponse(upstream), { status: upstream.status, headers });
  } catch {
    return new Response(null, { status: 502, headers: { "cache-control": "no-store" } });
  }
}

export function centralAuthResponseHeaders(responseHeaders: Headers): Headers {
  const result = new Headers();
  for (const name of RESPONSE_HEADERS) {
    if (name === "set-cookie") {
      for (const value of setCookieValues(responseHeaders)) result.append(name, value);
      continue;
    }
    const value = responseHeaders.get(name);
    if (value !== null) result.set(name, value);
  }
  if (result.get("cache-control") === null) result.set("cache-control", "no-store");
  return result;
}

function hasExpectedMethod(route: CentralAuthRoute, method: string): boolean {
  return (route === "login-start" || route === "logout") ? method === "POST" : method === "GET";
}

function requestHeaderNames(route: CentralAuthRoute): readonly string[] {
  if (route === "login-start") return ["origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest"];
  if (route === "logout") return REQUEST_HEADERS;
  return ["cookie"];
}

async function hasEmptyBody(request: Request): Promise<boolean> {
  const declared = request.headers.get("content-length");
  if (declared !== null && declared !== "0") return false;
  if (!request.body) return true;
  const reader = request.body.getReader();
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) return true;
      if (chunk.value.byteLength !== 0) return false;
    }
  } catch {
    return false;
  } finally {
    reader.releaseLock();
  }
}

function hasSelfClaim(headers: Headers): boolean {
  return Array.from(headers.keys()).some((header) => SELF_CLAIM_HEADERS.has(header.toLowerCase()));
}

function hasExpectedBrowserFetch(route: CentralAuthRoute, headers: Headers, publicOrigin: string): boolean {
  if (headers.get("origin") !== publicOrigin || headers.get("sec-fetch-site") !== "same-origin") return false;
  if (route === "login-start") {
    return headers.get("sec-fetch-mode") === "navigate" && headers.get("sec-fetch-dest") === "document";
  }
  return headers.get("sec-fetch-mode") === "cors" && headers.get("sec-fetch-dest") === "empty";
}

function hasExactCallbackQuery(url: URL): boolean {
  const values = Array.from(url.searchParams.entries());
  if (values.length !== 2) return false;
  const names = new Set(values.map(([name]) => name));
  if (!(names.size === 2 && names.has("state") && (names.has("code") || names.has("error")))) return false;
  const lookup = new Map(values);
  const state = lookup.get("state");
  const first = lookup.get(names.has("code") ? "code" : "error");
  return typeof state === "string" && state.length >= 1 && state.length <= 1024 &&
    typeof first === "string" && first.length >= 1 && first.length <= (names.has("code") ? 2048 : 128);
}

function hasSafeCallbackLocation(location: string | null): boolean {
  return location === "/ask";
}

function hasSafeLoginStartLocation(location: string | null): boolean {
  if (location === null || /[\\\u0000-\u001f\u007f]/.test(location)) return false;
  try {
    const target = new URL(location);
    return target.protocol === "https:" && target.hostname.length > 0 && !target.username && !target.password;
  } catch {
    return false;
  }
}

function setCookieValues(headers: Headers): string[] {
  const supported = headers as Headers & { getSetCookie?: () => string[]; raw?: () => Record<string, string[]> };
  if (typeof supported.getSetCookie === "function") return supported.getSetCookie();
  return supported.raw?.()["set-cookie"] ?? [];
}

async function readBoundedAuthResponse(response: Response): Promise<ArrayBuffer> {
  const declared = response.headers.get("content-length");
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_AUTH_RESPONSE_BYTES)) {
    throw new RangeError("response body too large");
  }
  if (response.body === null) return new ArrayBuffer(0);
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      length += chunk.value.byteLength;
      if (length > MAX_AUTH_RESPONSE_BYTES) throw new RangeError("response body too large");
      chunks.push(chunk.value);
    }
  } finally {
    reader.releaseLock();
  }
  const result = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result.buffer.slice(result.byteOffset, result.byteOffset + result.byteLength) as ArrayBuffer;
}
