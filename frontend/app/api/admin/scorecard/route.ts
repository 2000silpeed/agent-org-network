import { type NextRequest } from "next/server";
import { handleCentralAdminRoute } from "@/lib/central-admin-route";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest): Promise<Response> {
  return handleCentralAdminRoute("scorecard", request);
}
