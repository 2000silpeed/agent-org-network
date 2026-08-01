import { type NextRequest } from "next/server";
import { handleCentralAdminRoute } from "@/lib/central-admin-route";

export const dynamic = "force-dynamic";

type Context = { params: { card_id: string } };

export async function POST(request: NextRequest, context: Context): Promise<Response> {
  return handleCentralAdminRoute("revoke", request, context.params.card_id);
}
