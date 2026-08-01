import { type NextRequest } from "next/server";
import { handleCentralInboxRoute } from "@/lib/central-inbox-route";
export const dynamic = "force-dynamic";
type Context = { params: { reevaluation_id: string } };
export async function POST(request: NextRequest, context: Context): Promise<Response> {
  return handleCentralInboxRoute("reevaluation-disposition", request, context.params.reevaluation_id);
}
