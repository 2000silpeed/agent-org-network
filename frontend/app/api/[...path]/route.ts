// Server-side proxy for /api/* → FastAPI backend.
//
// Replaces next.config rewrites, which hang on POST request bodies in the dev
// server. This route runs on the Next server (Node), reads the body, and
// forwards method, headers, body, and cookies to the backend; the backend's
// Set-Cookie (anonymous session aon_uid) is passed back to the browser. Same
// origin, so no CORS and the httponly cookie flows transparently.

import { type NextRequest } from "next/server";
import {
  bffRequestHeaders,
  bffResponseHeaders,
  isAllowedBffRequest,
  readBffBody,
} from "@/lib/bff-policy";
import { readFrontendRuntimeConfig, upstreamUrl } from "@/lib/frontend-runtime";

export const dynamic = "force-dynamic";

async function proxy(req: NextRequest, path: string[]): Promise<Response> {
  if (!isAllowedBffRequest(req.method, path)) {
    return Response.json({ detail: "Not found" }, { status: 404 });
  }
  const runtime = readFrontendRuntimeConfig();
  if (!runtime.ok) {
    return Response.json({ detail: "Frontend runtime is not configured" }, { status: 503 });
  }
  const target = upstreamUrl(runtime.config, path, req.nextUrl.search);

  const headers = bffRequestHeaders(req.headers);

  const init: RequestInit = { method: req.method, headers, redirect: "manual" };
  try {
    const body = await readBffBody(req);
    if (body !== null) init.body = body as unknown as BodyInit;
  } catch (error) {
    if (error instanceof RangeError) {
      return Response.json({ detail: "Request body too large" }, { status: 413 });
    }
    return Response.json({ detail: "Invalid request body" }, { status: 400 });
  }

  let backendRes: Response;
  try {
    backendRes = await fetch(target, init);
  } catch {
    return new Response(
      JSON.stringify({ type: "error", message: "백엔드에 연결할 수 없습니다." }),
      { status: 502, headers: { "content-type": "application/json" } },
    );
  }

  // SSE (text/event-stream) must be piped through, NOT buffered — arrayBuffer()
  // would wait for the whole response and defeat token-by-token streaming.
  // Pass the backend's ReadableStream straight to the browser (same origin).
  const contentType = backendRes.headers.get("content-type") ?? "";
  const respHeaders = bffResponseHeaders(backendRes.headers, contentType.includes("text/event-stream"));
  if (contentType.includes("text/event-stream") && backendRes.body) {
    return new Response(backendRes.body, {
      status: backendRes.status,
      headers: respHeaders,
    });
  }

  const body = await backendRes.arrayBuffer();
  return new Response(body, { status: backendRes.status, headers: respHeaders });
}

type Ctx = { params: { path: string[] } };

export async function GET(req: NextRequest, ctx: Ctx): Promise<Response> {
  return proxy(req, ctx.params.path);
}

export async function POST(req: NextRequest, ctx: Ctx): Promise<Response> {
  return proxy(req, ctx.params.path);
}

export async function PUT(req: NextRequest, ctx: Ctx): Promise<Response> {
  return proxy(req, ctx.params.path);
}

export async function DELETE(req: NextRequest, ctx: Ctx): Promise<Response> {
  return proxy(req, ctx.params.path);
}
