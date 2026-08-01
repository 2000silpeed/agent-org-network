import { type NextRequest } from "next/server";
import { handleCentralAdmissionRoute } from "@/lib/central-admission-route";

export const dynamic = "force-dynamic";
export async function GET(request: NextRequest): Promise<Response> { return handleCentralAdmissionRoute("onboarding-status", request); }
