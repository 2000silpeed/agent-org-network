export const dynamic = "force-dynamic";

/** Node process liveness only; backend availability belongs to /readyz. */
export function GET(): Response {
  return Response.json({ status: "ok" });
}
