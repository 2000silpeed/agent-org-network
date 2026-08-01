import { type NextRequest } from "next/server";
import { handleCentralPolicyRoute } from "@/lib/central-policy-route";
export const dynamic = "force-dynamic";
export async function GET(request: NextRequest): Promise<Response> { return handleCentralPolicyRoute("policy-get", request); }
