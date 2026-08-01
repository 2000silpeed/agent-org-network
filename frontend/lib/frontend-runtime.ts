export type FrontendMode = "development" | "production" | "central-local-reference";

export interface FrontendRuntimeConfig {
  mode: FrontendMode;
  backendUrl: URL;
  publicOrigin: URL | null;
}

export type RuntimeConfigResult =
  | { ok: true; config: FrontendRuntimeConfig }
  | { ok: false; error: string };

const DEVELOPMENT_BACKEND = "http://127.0.0.1:8011";
const SAFE_UPSTREAM_PATH_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;

function urlFrom(value: string, name: string): URL | RuntimeConfigResult {
  try {
    const url = new URL(value);
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    ) {
      return { ok: false, error: `${name} 형식이 안전하지 않습니다.` };
    }
    return url;
  } catch {
    return { ok: false, error: `${name}은(는) 절대 URL이어야 합니다.` };
  }
}

function isLoopbackHost(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/^\[|\]$/g, "");
  return host === "localhost" || host === "::1" || /^127(?:\.\d{1,3}){3}$/.test(host);
}

/**
 * Validates only runtime inputs.  It is deliberately not evaluated at build
 * time: a standalone image is built once and receives its environment at run
 * time.
 */
export function readFrontendRuntimeConfig(
  env: Record<string, string | undefined> = process.env,
): RuntimeConfigResult {
  const rawMode = env.AON_FRONTEND_MODE ?? "development";
  if (rawMode !== "development" && rawMode !== "production" && rawMode !== "central-local-reference") {
    return { ok: false, error: "AON_FRONTEND_MODE는 development 또는 production이어야 합니다." };
  }
  const mode: FrontendMode = rawMode;
  const backendValue = env.AON_BACKEND_URL ?? (mode === "development" ? DEVELOPMENT_BACKEND : "");
  if (!backendValue) {
    return { ok: false, error: "production에서는 AON_BACKEND_URL이 필요합니다." };
  }
  const backend = urlFrom(backendValue, "AON_BACKEND_URL");
  if ("ok" in backend) return backend;

  if (mode === "development" && backend.protocol === "http:" && !isLoopbackHost(backend.hostname)) {
    return { ok: false, error: "development HTTP backend는 loopback에서만 허용됩니다." };
  }

  if (mode === "development") {
    return { ok: true, config: { mode, backendUrl: backend, publicOrigin: null } };
  }

  if (mode === "central-local-reference") {
    if (backend.toString() !== "http://127.0.0.1:8010/") {
      return { ok: false, error: "central-local-reference backend는 고정 loopback이어야 합니다." };
    }
    const publicValue = env.AON_PUBLIC_ORIGIN;
    if (!publicValue) return { ok: false, error: "central-local-reference에는 AON_PUBLIC_ORIGIN이 필요합니다." };
    const publicOrigin = urlFrom(publicValue, "AON_PUBLIC_ORIGIN");
    if ("ok" in publicOrigin) return publicOrigin;
    if (publicOrigin.protocol !== "https:" || publicOrigin.pathname !== "/") {
      return { ok: false, error: "AON_PUBLIC_ORIGIN은 HTTPS origin이어야 합니다." };
    }
    return { ok: true, config: { mode, backendUrl: backend, publicOrigin } };
  }

  const publicValue = env.AON_PUBLIC_ORIGIN;
  if (!publicValue) {
    return { ok: false, error: "production에서는 AON_PUBLIC_ORIGIN이 필요합니다." };
  }
  const publicOrigin = urlFrom(publicValue, "AON_PUBLIC_ORIGIN");
  if ("ok" in publicOrigin) return publicOrigin;
  if (publicOrigin.protocol !== "https:") {
    return { ok: false, error: "AON_PUBLIC_ORIGIN은 HTTPS origin이어야 합니다." };
  }
  if (publicOrigin.pathname !== "/") {
    return { ok: false, error: "AON_PUBLIC_ORIGIN에는 path를 넣을 수 없습니다." };
  }
  return { ok: true, config: { mode, backendUrl: backend, publicOrigin } };
}

export function upstreamUrl(config: FrontendRuntimeConfig, path: string[], search: string): URL {
  if (
    !Array.isArray(path) ||
    path.length === 0 ||
    !path.every((segment) => typeIsSafeUpstreamPathSegment(segment))
  ) {
    throw new RangeError("upstream path segment is unsafe");
  }
  const target = new URL(config.backendUrl.toString());
  const basePath = target.pathname.replace(/\/$/, "");
  // Each segment is an ASCII opaque token, so WHATWG URL normalization cannot
  // reinterpret it as a dot segment or a decoded path separator.
  target.pathname = `${basePath}/${path.join("/")}`;
  target.search = search;
  return target;
}

function typeIsSafeUpstreamPathSegment(value: unknown): value is string {
  return typeIsString(value) && SAFE_UPSTREAM_PATH_SEGMENT.test(value);
}

function typeIsString(value: unknown): value is string {
  return typeof value === "string";
}
