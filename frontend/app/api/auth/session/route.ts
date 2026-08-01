import { type NextRequest } from "next/server";
import { handleCentralAuthRoute } from "@/lib/central-auth-route";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest): Promise<Response> {
  return handleCentralAuthRoute("session", request);
}
