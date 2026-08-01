import { type NextRequest } from "next/server";
import { handleCentralInboxRoute } from "@/lib/central-inbox-route";
export const dynamic = "force-dynamic";
type Context = { params: { case_id: string } };
export async function GET(request: NextRequest, context: Context): Promise<Response> {
  return handleCentralInboxRoute("conflict-detail", request, context.params.case_id);
}
