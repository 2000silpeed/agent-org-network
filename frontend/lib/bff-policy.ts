export const MAX_BFF_BODY_BYTES = 1_048_576;

export function isAllowedBffRequest(method: string, path: string[]): boolean {
  // Question lifecycle is mounted only by the four dedicated handlers under
  // /api/questions.  This legacy catch-all must never become a fallback
  // route, even when a caller spells a formerly supported private path.
  void method;
  void path;
  return false;
}

const REQUEST_HEADERS = ["accept", "content-type", "cookie", "idempotency-key"] as const;
const RESPONSE_HEADERS = ["cache-control", "content-type", "set-cookie", "x-request-id"] as const;

export function bffRequestHeaders(requestHeaders: Headers): Headers {
  const result = new Headers();
  for (const name of REQUEST_HEADERS) {
    const value = requestHeaders.get(name);
    if (value !== null) result.set(name, value);
  }
  return result;
}

export function bffResponseHeaders(responseHeaders: Headers, isSse: boolean): Headers {
  const result = new Headers();
  for (const name of RESPONSE_HEADERS) {
    const value = responseHeaders.get(name);
    if (value !== null) result.set(name, value);
  }
  if (isSse) {
    result.set("cache-control", "no-cache, no-transform");
    result.set("x-accel-buffering", "no");
  }
  return result;
}

export async function readBffBody(request: Request): Promise<Uint8Array | null> {
  if (request.method === "GET" || request.method === "HEAD") return null;
  const declared = request.headers.get("content-length");
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_BFF_BODY_BYTES)) {
    throw new RangeError("request body too large");
  }
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      length += next.value.byteLength;
      if (length > MAX_BFF_BODY_BYTES) throw new RangeError("request body too large");
      chunks.push(next.value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}
