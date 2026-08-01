import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

async function loadModule() {
  const source = await readFile(new URL("./central-policy-bff.ts", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 }, fileName: "central-policy-bff.ts" });
  return import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
}
const policy = await loadModule();
const ORIGIN = "https://central.example.test";
const base = { host: "central.example.test", cookie: "__Host-aon-central-session=s", accept: "application/json" };
const post = { ...base, origin: ORIGIN, "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "x-aon-csrf": "c".repeat(43), "idempotency-key": "policy-1", "content-type": "application/json" };

test("policy GET/POST는 exact private upstream와 caller claim 차단을 유지한다", async () => {
  assert.equal(policy.policyUpstreamUrl("policy-get").pathname, "/v1/admin/policy");
  assert.equal(policy.policyUpstreamUrl("policy-post").pathname, "/v1/admin/policy/revisions");
  assert.equal(await policy.isValidPolicyRequest("policy-get", new Request(`${ORIGIN}/api/admin/policy`, { headers: base }), ORIGIN), true);
  assert.equal(await policy.isValidPolicyRequest("policy-get", new Request(`${ORIGIN}/api/admin/policy?x=1`, { headers: base }), ORIGIN), false);
  assert.equal(await policy.isValidPolicyRequest("policy-post", new Request(`${ORIGIN}/api/admin/policy/revisions`, { method: "POST", headers: post, body: "{}" }), ORIGIN), true);
  assert.equal(await policy.isValidPolicyRequest("policy-post", new Request(`${ORIGIN}/api/admin/policy/revisions`, { method: "POST", headers: { ...post, authorization: "Bearer forged" }, body: "{}" }), ORIGIN), false);
});

test("policy BFF는 identity claim을 upstream에 전달하지 않는다", () => {
  const forwarded = policy.policyRequestHeaders("policy-post", new Headers({ ...post, authorization: "secret", "x-aon-role": "admin" }));
  assert.equal(forwarded.get("cookie"), base.cookie);
  assert.equal(forwarded.get("idempotency-key"), "policy-1");
  assert.equal(forwarded.get("authorization"), null);
  assert.equal(forwarded.get("x-aon-role"), null);
});
