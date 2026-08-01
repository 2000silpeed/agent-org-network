import { type NextRequest } from "next/server";
import {
  authUpstreamUrl,
  centralAuthRequestHeaders,
  isValidCentralAuthRequest,
  relayCentralAuthResponse,
  type CentralAuthRoute,
} from "@/lib/central-auth-bff";
import { readCentralAuthPublicOrigin } from "@/lib/central-auth-runtime";

export async function handleCentralAuthRoute(route: CentralAuthRoute, request: NextRequest): Promise<Response> {
  const publicOrigin = readCentralAuthPublicOrigin();
  if (publicOrigin === null) return new Response(null, { status: 503, headers: { "cache-control": "no-store" } });
  if (!await isValidCentralAuthRequest(route, request, publicOrigin)) {
    return new Response(null, { status: route === "login-start" || route === "logout" ? 403 : 400, headers: { "cache-control": "no-store" } });
  }
  try {
    const upstream = await fetch(authUpstreamUrl(route, request.nextUrl.search), {
      method: request.method,
      headers: centralAuthRequestHeaders(route, request.headers),
      redirect: "manual",
      cache: "no-store",
    });
    return relayCentralAuthResponse(route, upstream);
  } catch {
    return new Response(null, { status: 503, headers: { "cache-control": "no-store" } });
  }
}
