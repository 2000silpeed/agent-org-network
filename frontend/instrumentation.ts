import { readFrontendRuntimeConfig } from "@/lib/frontend-runtime";

// A standalone artifact is configured when its Node process starts, not when
// the image is built. Invalid production settings must therefore stop startup.
export async function register(): Promise<void> {
  const runtime = readFrontendRuntimeConfig();
  if (!runtime.ok) {
    throw new Error(`AON frontend runtime configuration invalid: ${runtime.error}`);
  }
}
