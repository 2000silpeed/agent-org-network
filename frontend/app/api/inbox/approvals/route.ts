import { type NextRequest } from "next/server";
import { handleCentralInboxRoute } from "@/lib/central-inbox-route";
export const dynamic = "force-dynamic";
export async function GET(request: NextRequest): Promise<Response> {
  return handleCentralInboxRoute("approval-list", request);
}
