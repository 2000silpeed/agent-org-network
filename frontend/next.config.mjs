/** @type {import('next').NextConfig} */

// /api/* is proxied to the FastAPI backend by a server-side route handler
// (app/api/[...path]/route.ts), not next.config rewrites — the dev rewrite
// proxy hangs on POST request bodies. The route handler forwards method, body,
// and cookies (anonymous session aon_uid flows transparently, same origin).
const nextConfig = {};

export default nextConfig;
