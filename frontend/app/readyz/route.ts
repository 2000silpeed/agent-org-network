import { readFrontendRuntimeConfig, upstreamUrl } from "@/lib/frontend-runtime";

export const dynamic = "force-dynamic";

const READY_TIMEOUT_MS = 2_000;

/** Runtime configuration plus the private Developer API's readiness. */
export async function GET(): Promise<Response> {
  const runtime = readFrontendRuntimeConfig();
  if (!runtime.ok) {
    return Response.json({ status: "not_ready" }, { status: 503 });
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), READY_TIMEOUT_MS);
  try {
    const response = await fetch(upstreamUrl(runtime.config, ["readyz"], ""), {
      headers: { accept: "application/json" },
      cache: "no-store",
      signal: controller.signal,
    });
    return Response.json(
      { status: response.ok ? "ready" : "not_ready" },
      { status: response.ok ? 200 : 503 },
    );
  } catch {
    return Response.json({ status: "not_ready" }, { status: 503 });
  } finally {
    clearTimeout(timeout);
  }
}
