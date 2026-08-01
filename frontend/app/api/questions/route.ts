import { type NextRequest } from "next/server";
import { handleCentralQuestionRoute } from "@/lib/central-question-route";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest): Promise<Response> {
  return handleCentralQuestionRoute("create", request);
}
