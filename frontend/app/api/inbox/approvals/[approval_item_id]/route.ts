import { type NextRequest } from "next/server";
import { handleCentralInboxRoute } from "@/lib/central-inbox-route";
export const dynamic = "force-dynamic";
type Context = { params: { approval_item_id: string } };
export async function GET(request: NextRequest, context: Context): Promise<Response> {
  return handleCentralInboxRoute("approval-detail", request, context.params.approval_item_id);
}
