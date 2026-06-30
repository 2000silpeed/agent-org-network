// Server-side proxy for /api/* → FastAPI backend.
//
// Replaces next.config rewrites, which hang on POST request bodies in the dev
// server. This route runs on the Next server (Node), reads the body, and
// forwards method, headers, body, and cookies to the backend; the backend's
// Set-Cookie (anonymous session aon_uid) is passed back to the browser. Same
// origin, so no CORS and the httponly cookie flows transparently.

import { type NextRequest } from "next/server";

export const dynamic = "force-dynamic";

// 127.0.0.1 (not "localhost") — Node fetch resolves "localhost" to IPv6 (::1)
// first and waits ~10s for the IPv4 fallback when uvicorn binds 127.0.0.1 only.
const BACKEND_URL = process.env.AON_BACKEND_URL ?? "http://127.0.0.1:8011";

async function proxy(req: NextRequest, path: string[]): Promise<Response> {
  const search = req.nextUrl.search;
  const target = `${BACKEND_URL}/${path.join("/")}${search}`;

  const headers = new Headers(req.headers);
  headers.delete("host");
  headers.delete("connection");
  headers.delete("content-length");

  const init: RequestInit = { method: req.method, headers, redirect: "manual" };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = await req.text();
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

  const respHeaders = new Headers(backendRes.headers);
  respHeaders.delete("content-encoding");
  respHeaders.delete("content-length");
  respHeaders.delete("transfer-encoding");

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
