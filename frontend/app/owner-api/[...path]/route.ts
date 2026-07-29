import { type NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 140 * 1024 * 1024;

function ownerBackendUrl(): URL | null {
  const configured = process.env.AON_OWNER_BACKEND_URL;
  if (!configured) return null;
  try {
    const url = new URL(configured);
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.search ||
      url.hash ||
      url.pathname.includes("..") ||
      (url.pathname !== "/" && !/^\/[A-Za-z0-9/_-]*$/.test(url.pathname))
    ) return null;
    return url;
  } catch {
    return null;
  }
}

export async function POST(
  request: NextRequest,
  context: { params: { path: string[] } },
): Promise<Response> {
  if (
    context.params.path.length !== 2 ||
    context.params.path[0] !== "authoring" ||
    context.params.path[1] !== "runs" ||
    request.nextUrl.search !== ""
  ) return Response.json({ detail: "Not found" }, { status: 404 });
  const backend = ownerBackendUrl();
  if (backend === null) {
    return Response.json(
      { detail: "Card Owner installation unavailable" },
      { status: 503 },
    );
  }
  const declaredLength = request.headers.get("content-length");
  if (
    declaredLength === null ||
    !/^[0-9]+$/.test(declaredLength) ||
    Number(declaredLength) < 1 ||
    Number(declaredLength) > MAX_BODY_BYTES
  ) return Response.json({ detail: "Invalid request" }, { status: 422 });
  const headers = new Headers();
  for (const name of ["accept", "content-type", "idempotency-key", "x-owner-csrf", "origin"]) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  const ownerSession = request.cookies.get("aon_owner_session")?.value;
  if (ownerSession) headers.set("cookie", `aon_owner_session=${ownerSession}`);
  try {
    const body = await request.arrayBuffer();
    if (body.byteLength !== Number(declaredLength) || body.byteLength > MAX_BODY_BYTES) {
      return Response.json({ detail: "Invalid request" }, { status: 422 });
    }
    headers.set("content-length", String(body.byteLength));
    const basePath = backend.pathname.replace(/\/$/, "");
    backend.pathname = `${basePath}/authoring/runs`;
    const response = await fetch(backend.toString(), {
      method: "POST",
      headers,
      body,
      redirect: "manual",
    });
    const outputHeaders = new Headers();
    const responseContentType = response.headers.get("content-type");
    if (responseContentType !== null) {
      outputHeaders.set("content-type", responseContentType);
    }
    return new Response(await response.arrayBuffer(), {
      status: response.status,
      headers: outputHeaders,
    });
  } catch {
    return Response.json(
      { detail: "Card Owner installation unavailable" },
      { status: 503 },
    );
  }
}
