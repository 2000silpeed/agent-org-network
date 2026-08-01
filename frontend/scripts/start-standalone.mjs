// Node 24 strips the type-only syntax in this small server-only helper.  Keep
// this bootstrap outside Next's instrumentation hook: Next logs hook failures
// but continues its process, while an invalid production configuration must
// fail before the standalone server binds a browser port.
import { readFrontendRuntimeConfig } from "../lib/frontend-runtime.ts";
import { existsSync } from "node:fs";

const runtime = readFrontendRuntimeConfig();
if (!runtime.ok) {
  console.error(`AON frontend runtime configuration invalid: ${runtime.error}`);
  process.exitCode = 1;
} else {
  const localServer = new URL("../.next/standalone/server.js", import.meta.url);
  const server = existsSync(localServer) ? localServer : new URL("../server.js", import.meta.url);
  await import(server.href);
}
