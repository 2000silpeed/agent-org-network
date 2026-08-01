import { type NextRequest } from "next/server";
import { isValidPolicyRequest, policyRequestHeaders, policyUpstreamUrl, readPolicyBody, relayPolicyResponse, type PolicyRoute } from "@/lib/central-policy-bff";
import { readCentralAuthPublicOrigin } from "@/lib/central-auth-runtime";

export async function handleCentralPolicyRoute(route: PolicyRoute, request: NextRequest): Promise<Response> {
  const origin = readCentralAuthPublicOrigin();
  if (origin === null || !await isValidPolicyRequest(route, request, origin)) return new Response(JSON.stringify({ error: "central_policy_forbidden" }), { status: origin === null ? 503 : 403, headers: { "cache-control": "no-store", "content-type": "application/json" } });
  try {
    const body = route === "policy-post" ? await readPolicyBody(request) : undefined;
    const upstream = await fetch(policyUpstreamUrl(route), { method: request.method, headers: policyRequestHeaders(route, request.headers), body: body as unknown as BodyInit | undefined, redirect: "manual", cache: "no-store" });
    return relayPolicyResponse(upstream);
  } catch { return new Response(JSON.stringify({ error: "central_policy_unavailable" }), { status: 502, headers: { "cache-control": "no-store", "content-type": "application/json" } }); }
}
