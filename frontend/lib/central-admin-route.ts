import { type NextRequest } from "next/server";
import { handleCentralAdminBff, type CentralAdminRoute } from "@/lib/central-admin-bff";
import { readCentralAuthPublicOrigin } from "@/lib/central-auth-runtime";

/** One fixed Central endpoint per graph/ownership/scorecard route. */
export async function handleCentralAdminRoute(
  route: CentralAdminRoute, request: NextRequest, cardId?: string,
): Promise<Response> {
  const origin = readCentralAuthPublicOrigin();
  if (origin === null) {
    return new Response(JSON.stringify({ code: "unavailable", message: "관리자 서비스를 지금 사용할 수 없습니다." }), {
      status: 503,
      headers: { "cache-control": "no-store", "content-type": "application/json; charset=utf-8" },
    });
  }
  return handleCentralAdminBff(route, request, origin, cardId);
}
