import { type NextRequest } from "next/server";
import { handleCentralInboxBff, type CentralInboxRoute } from "@/lib/central-inbox-bff";
import { readCentralAuthPublicOrigin } from "@/lib/central-auth-runtime";

export async function handleCentralInboxRoute(
  route: CentralInboxRoute, request: NextRequest, aggregateId?: string,
): Promise<Response> {
  const origin = readCentralAuthPublicOrigin();
  if (origin === null) {
    return new Response(JSON.stringify({ code: "unavailable", message: "처리함 서비스를 지금 사용할 수 없습니다." }), {
      status: 503,
      headers: { "cache-control": "no-store", "content-type": "application/json; charset=utf-8" },
    });
  }
  return handleCentralInboxBff(route, request, origin, aggregateId);
}
