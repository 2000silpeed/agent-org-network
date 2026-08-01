import { type NextRequest } from "next/server";
import { handleCentralInboxRoute } from "@/lib/central-inbox-route";
export const dynamic = "force-dynamic";
type Context = { params: { review_id: string } };
export async function POST(request: NextRequest, context: Context): Promise<Response> {
  return handleCentralInboxRoute("backup-disposition", request, context.params.review_id);
}
