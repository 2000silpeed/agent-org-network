import { type NextRequest } from "next/server";
import { handleCentralQuestionRoute } from "@/lib/central-question-route";

export const dynamic = "force-dynamic";

type Context = { params: { request_id: string } };

export async function POST(request: NextRequest, context: Context): Promise<Response> {
  return handleCentralQuestionRoute("feedback", request, context.params.request_id);
}
