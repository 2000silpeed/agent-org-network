import { readFrontendRuntimeConfig } from "@/lib/frontend-runtime";

/** Browser auth has no development identity fallback: it needs a configured HTTPS origin. */
export function readCentralAuthPublicOrigin(): string | null {
  const runtime = readFrontendRuntimeConfig();
  if (!runtime.ok || runtime.config.publicOrigin === null) return null;
  return runtime.config.publicOrigin.origin;
}
