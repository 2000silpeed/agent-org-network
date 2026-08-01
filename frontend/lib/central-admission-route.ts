import { type NextRequest } from "next/server";
import { admissionRequestHeaders, admissionUnavailable, admissionUpstreamUrl, isValidAdmissionRequest, readAdmissionBody, relayAdmissionResponse, type AdmissionRoute } from "@/lib/central-admission-bff";
import { readCentralAuthPublicOrigin } from "@/lib/central-auth-runtime";

export async function handleCentralAdmissionRoute(route: AdmissionRoute, request: NextRequest): Promise<Response> {
  const origin = readCentralAuthPublicOrigin();
  if (origin === null) return new Response(JSON.stringify({ error: "central_admission_unavailable" }), { status: 503, headers: { "cache-control": "no-store", "content-type": "application/json" } });
  if (!await isValidAdmissionRequest(route, request, origin)) return new Response(JSON.stringify({ error: "central_admission_forbidden" }), { status: request.method === "POST" ? 403 : 400, headers: { "cache-control": "no-store", "content-type": "application/json" } });
  try {
    const body = request.method === "POST" ? await readAdmissionBody(request) : undefined;
    const upstream = await fetch(admissionUpstreamUrl(route), { method: request.method, headers: admissionRequestHeaders(route, request.headers), body: body as unknown as BodyInit | undefined, redirect: "manual", cache: "no-store" });
    return relayAdmissionResponse(upstream);
  } catch { return admissionUnavailable(); }
}
