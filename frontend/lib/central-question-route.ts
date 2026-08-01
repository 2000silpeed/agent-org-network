import { type NextRequest } from "next/server";
import { handleCentralQuestionBff, type CentralQuestionRoute } from "@/lib/central-question-bff";
import { readCentralAuthPublicOrigin } from "@/lib/central-auth-runtime";

/** One fixed Central endpoint per public Question lifecycle route. */
export async function handleCentralQuestionRoute(
  route: CentralQuestionRoute, request: NextRequest, requestId?: string,
): Promise<Response> {
  const origin = readCentralAuthPublicOrigin();
  if (origin === null) {
    return new Response(JSON.stringify({ error: "central_question_unavailable", message: "질문 서비스를 지금 사용할 수 없습니다." }), {
      status: 503,
      headers: { "cache-control": "no-store", "content-type": "application/json; charset=utf-8" },
    });
  }
  return handleCentralQuestionBff(route, request, origin, requestId);
}
