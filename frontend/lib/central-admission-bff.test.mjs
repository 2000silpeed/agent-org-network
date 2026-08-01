import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("./central-admission-bff.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  fileName: "central-admission-bff.ts",
  reportDiagnostics: true,
});
assert.deepEqual(compiled.diagnostics ?? [], []);
const admission = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);

const ORIGIN = "https://central.example.test";
const COOKIE = "__Host-aon-central-session=opaque";
const CSRF = "a".repeat(43);
// Fetch's synthetic Request object does not materialize Host; an actual HTTP
// request always does, and the BFF binds admission to that ingress host.
const baseHeaders = { Cookie: COOKIE, Host: "central.example.test" };
const postHeaders = {
  ...baseHeaders,
  Origin: ORIGIN,
  "Sec-Fetch-Site": "same-origin",
  "Sec-Fetch-Mode": "cors",
  "Sec-Fetch-Dest": "empty",
  "X-AON-CSRF": CSRF,
  "Idempotency-Key": "request-123",
  "Content-Type": "application/json",
};

function request(path, init = {}) {
  return new Request(`${ORIGIN}${path}`, init);
}

test("admission BFF는 다섯 개 exact method/path만 fixed Central로 매핑한다", () => {
  assert.equal(admission.admissionUpstreamUrl("onboarding-status").toString(), "http://127.0.0.1:8010/onboarding/status");
  assert.equal(admission.admissionUpstreamUrl("users-get").toString(), "http://127.0.0.1:8010/admin/users");
  assert.equal(admission.admissionUpstreamUrl("users-post").toString(), "http://127.0.0.1:8010/admin/users");
  assert.equal(admission.admissionUpstreamUrl("cards-get").toString(), "http://127.0.0.1:8010/admin/agent-cards");
  assert.equal(admission.admissionUpstreamUrl("cards-post").toString(), "http://127.0.0.1:8010/admin/agent-cards");
  assert.equal(admission.admissionUpstreamUrl("users-post").host, "127.0.0.1:8010");
});

test("GET은 cookie-only, POST는 exact Origin/Fetch/CSRF/idempotency JSON만 받는다", async () => {
  assert.equal(await admission.isValidAdmissionRequest("onboarding-status", request("/api/onboarding/status", { headers: baseHeaders }), ORIGIN), true);
  assert.equal(await admission.isValidAdmissionRequest("users-get", request("/api/admin/users?user=forged", { headers: baseHeaders }), ORIGIN), false);
  assert.equal(await admission.isValidAdmissionRequest("cards-get", request("/api/admin/agent-cards", { headers: { ...baseHeaders, Authorization: "Bearer forged" } }), ORIGIN), false);
  assert.equal(await admission.isValidAdmissionRequest("users-post", request("/api/admin/users", { method: "POST", headers: postHeaders, body: "{}" }), ORIGIN), true);
  for (const headers of [
    { ...postHeaders, "Sec-Fetch-Mode": "navigate" },
    { ...postHeaders, Origin: "https://evil.example" },
    { ...postHeaders, "X-AON-CSRF": "wrong" },
    { ...postHeaders, "X-AON-Role": "admin" },
    { ...postHeaders, "X-Forwarded-Host": "evil.example" },
    { ...postHeaders, "X-Forwarded-For": "198.51.100.7" },
    { ...postHeaders, Forwarded: "host=evil.example" },
    { ...postHeaders, "X-AON-Session": "forged" },
    { ...postHeaders, "Content-Type": "text/plain" },
  ]) assert.equal(await admission.isValidAdmissionRequest("cards-post", request("/api/admin/agent-cards", { method: "POST", headers, body: "{}" }), ORIGIN), false);
  // Values that happen to agree with the request URL are still caller claims.
  // Only the standalone server's raw-header provenance guard may classify
  // Next's synthesized forwarding facts as trusted transport metadata.
  assert.equal(await admission.isValidAdmissionRequest("users-post", request("/api/admin/users", {
    method: "POST", headers: { ...postHeaders, "X-Forwarded-Host": "central.example.test" }, body: "{}",
  }), ORIGIN), false);
});

test("admission BFF는 allowlist proof와 64KiB streamed body limit을 유지한다", async () => {
  const relayed = admission.admissionRequestHeaders("cards-post", new Headers({ ...postHeaders, "X-Forwarded-Host": "evil", Authorization: "Bearer no", "X-Other": "no" }));
  assert.deepEqual([...relayed.keys()].sort(), ["content-type", "cookie", "idempotency-key", "origin", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "x-aon-csrf"]);
  const tooLarge = request("/api/admin/users", { method: "POST", headers: { ...postHeaders, "Content-Length": "65537" }, body: "{}" });
  await assert.rejects(admission.readAdmissionBody(tooLarge), RangeError);
  const valid = await admission.readAdmissionBody(request("/api/admin/users", { method: "POST", headers: postHeaders, body: "{}" }));
  assert.equal(new TextDecoder().decode(valid), "{}");
});

test("response는 no-store와 safe headers만 보존하고 초과/invalid upstream은 502로 닫는다", async () => {
  const relayed = await admission.relayAdmissionResponse(new Response('{"error":"safe"}', {
    status: 403,
    headers: { "content-type": "application/json", "x-leak": "no", "cache-control": "public, max-age=90" },
  }));
  assert.equal(relayed.status, 403);
  assert.equal(relayed.headers.get("cache-control"), "no-store");
  assert.equal(relayed.headers.get("x-leak"), null);
  const oversized = await admission.relayAdmissionResponse(new Response("x", { headers: { "content-length": "1048577" } }));
  assert.equal(oversized.status, 502);
  assert.deepEqual(await oversized.json(), { error: "central_admission_unavailable" });
});
