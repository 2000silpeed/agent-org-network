import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("./central-admin-bff.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  fileName: "central-admin-bff.ts",
  reportDiagnostics: true,
});
assert.deepEqual(compiled.diagnostics ?? [], []);
const admin = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);

const ORIGIN = "https://central.example.test";
const COOKIE = "__Host-aon-central-session=opaque-session; __Host-aon-central-csrf=csrf";
const CSRF = "a".repeat(43);
const baseGet = { Host: "central.example.test", Cookie: COOKIE };
const postHeaders = {
  ...baseGet,
  Origin: ORIGIN,
  "Sec-Fetch-Site": "same-origin",
  "Sec-Fetch-Mode": "cors",
  "Sec-Fetch-Dest": "empty",
  "X-AON-CSRF": CSRF,
  "Idempotency-Key": "admin-command-1",
  "Content-Type": "application/json",
};

const transfer = {
  new_owner_user_id: "user-2",
  expected_card_revision: 3,
  expected_assignment_generation: 2,
  expected_assignment_revision: 4,
};
const revoke = {
  reason_code: "operator_revoke",
  expected_card_revision: 3,
  expected_assignment_generation: 2,
  expected_assignment_revision: 4,
};

test("Admin BFF는 exact four public route를 fixed loopback /v1 pair로 매핑한다", () => {
  assert.equal(admin.centralAdminUpstreamUrl("org").toString(), "http://127.0.0.1:8010/v1/console/org");
  assert.equal(admin.centralAdminUpstreamUrl("owner-transfer", "card-1").toString(), "http://127.0.0.1:8010/v1/admin/agent-cards/card-1/owner-transfers");
  assert.equal(admin.centralAdminUpstreamUrl("revoke", "card-1").toString(), "http://127.0.0.1:8010/v1/admin/agent-cards/card-1/revocations");
  assert.equal(admin.centralAdminUpstreamUrl("scorecard").toString(), "http://127.0.0.1:8010/v1/admin/scorecard");
  assert.equal(admin.centralAdminUpstreamUrl("scorecard", undefined, new URLSearchParams({ since: "2026-08-01T00:00:00Z", until: "2026-08-02T00:00:00Z" })).search, "?since=2026-08-01T00%3A00%3A00Z&until=2026-08-02T00%3A00%3A00Z");
  assert.throws(() => admin.centralAdminUpstreamUrl("owner-transfer", "card/child"), RangeError);
  assert.throws(() => admin.centralAdminUpstreamUrl("org", "caller-selected"), RangeError);
});

test("GET은 cookie-only이고 scorecard query만 exact UTC window을 허용한다", async () => {
  assert.equal(await admin.isValidCentralAdminRequest("org", new Request(`${ORIGIN}/api/console/org`, { headers: baseGet }), ORIGIN), true);
  assert.equal(await admin.isValidCentralAdminRequest("scorecard", new Request(`${ORIGIN}/api/admin/scorecard?since=2026-08-01T00:00:00Z&until=2026-08-02T00:00:00Z`, { headers: baseGet }), ORIGIN), true);
  for (const request of [
    new Request(`${ORIGIN}/api/console/org?org=forged`, { headers: baseGet }),
    new Request(`${ORIGIN}/api/admin/scorecard?since=2026-08-01T00:00:00Z`, { headers: baseGet }),
    new Request(`${ORIGIN}/api/admin/scorecard?since=2026-08-01T00:00:00+00:00&until=2026-08-02T00:00:00Z`, { headers: baseGet }),
    new Request(`${ORIGIN}/api/admin/scorecard?role=admin`, { headers: baseGet }),
    new Request(`${ORIGIN}/api/console/org`, { headers: { ...baseGet, "X-AON-Role": "admin" } }),
    new Request(`${ORIGIN}/api/console/org`, { headers: { Host: "central.example.test" } }),
  ]) assert.equal(await admin.isValidCentralAdminRequest(request.url.includes("scorecard") ? "scorecard" : "org", request, ORIGIN), false);
});

test("POST는 same-origin Fetch/CSRF/idempotency와 exact actor-free DTO만 받는다", async () => {
  assert.equal(await admin.isValidCentralAdminRequest("owner-transfer", new Request(`${ORIGIN}/api/admin/agent-cards/card-1/owner-transfers`, { method: "POST", headers: postHeaders, body: JSON.stringify(transfer) }), ORIGIN, "card-1"), true);
  assert.equal(await admin.isValidCentralAdminRequest("revoke", new Request(`${ORIGIN}/api/admin/agent-cards/card-1/revocations`, { method: "POST", headers: postHeaders, body: JSON.stringify(revoke) }), ORIGIN, "card-1"), true);
  const invalid = [
    { ...postHeaders, Origin: "https://evil.example" },
    { ...postHeaders, "Sec-Fetch-Mode": "navigate" },
    { ...postHeaders, "X-AON-CSRF": "wrong" },
    { ...postHeaders, "Idempotency-Key": "" },
    { ...postHeaders, "Content-Type": "text/plain" },
    { ...postHeaders, "X-AON-Org": "forged" },
  ];
  for (const headers of invalid) assert.equal(await admin.isValidCentralAdminRequest("owner-transfer", new Request(`${ORIGIN}/api/admin/agent-cards/card-1/owner-transfers`, { method: "POST", headers, body: JSON.stringify(transfer) }), ORIGIN, "card-1"), false);
  assert.equal(await admin.isValidCentralAdminRequest("owner-transfer", new Request(`${ORIGIN}/api/admin/agent-cards/card-1/owner-transfers`, { method: "POST", headers: postHeaders, body: JSON.stringify({ ...transfer, actor: "forged" }) }), ORIGIN, "card-1"), false);
  assert.equal(await admin.isValidCentralAdminRequest("revoke", new Request(`${ORIGIN}/api/admin/agent-cards/card-1/revocations`, { method: "POST", headers: postHeaders, body: JSON.stringify({ ...revoke, reason_code: "free text" }) }), ORIGIN, "card-1"), false);
});

test("body/response bound와 safe no-store projection을 지킨다", async () => {
  const body = await admin.readCentralAdminBody("owner-transfer", new Request(`${ORIGIN}/api/admin/agent-cards/card-1/owner-transfers`, { method: "POST", headers: postHeaders, body: JSON.stringify(transfer) }));
  assert.deepEqual(JSON.parse(new TextDecoder().decode(body)), transfer);
  await assert.rejects(admin.readCentralAdminBody("revoke", new Request(`${ORIGIN}/api/admin/agent-cards/card-1/revocations`, { method: "POST", headers: { ...postHeaders, "Content-Length": "65537" }, body: JSON.stringify(revoke) })), RangeError);
  const graph = { registry_revision: 3, policy_epoch: 2, policy_digest: "a".repeat(64), source_digest: "b".repeat(64), nodes: [{ kind: "user", user_id: "user-1", manager_user_id: null }], edges: [] };
  const relayed = await admin.relayCentralAdminResponse("org", new Response(JSON.stringify(graph), { status: 200, headers: { "content-type": "application/json", "set-cookie": "leak=1" } }));
  assert.equal(relayed.status, 200);
  assert.deepEqual(await relayed.json(), graph);
  assert.equal(relayed.headers.get("cache-control"), "no-store");
  assert.equal(relayed.headers.get("set-cookie"), null);
  const unsafe = await admin.relayCentralAdminResponse("org", new Response(JSON.stringify({ secret: "leak" }), { status: 200 }));
  assert.equal(unsafe.status, 502);
  assert.deepEqual(await unsafe.json(), { code: "unavailable", message: "관리자 서비스를 지금 사용할 수 없습니다." });
});

test("BFF invalid request는 backend reach 0이고 fetch는 fixed URL/allowlist header만 받는다", async () => {
  let calls = 0;
  const fetcher = async (url, init) => {
    calls += 1;
    assert.equal(url.toString(), "http://127.0.0.1:8010/v1/admin/agent-cards/card-1/owner-transfers");
    assert.deepEqual([...init.headers.keys()].sort(), ["content-type", "cookie", "idempotency-key", "origin", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "x-aon-csrf"]);
    return new Response(JSON.stringify({ receipt_id: "r-1", card_id: "card-1", card_revision: 4, registry_revision: 5, closed_assignment_id: "a-1", new_assignment_id: "a-2", new_generation: 3, new_assignment_revision: 1, from_owner_user_id: "user-1", to_owner_user_id: "user-2", policy_epoch: 2, policy_digest: "a".repeat(64), replayed: false }), { status: 201 });
  };
  const forged = await admin.handleCentralAdminBff("owner-transfer", new Request(`${ORIGIN}/api/admin/agent-cards/card-1/owner-transfers`, { method: "POST", headers: { ...postHeaders, "X-AON-Actor": "forged" }, body: JSON.stringify(transfer) }), ORIGIN, "card-1", fetcher);
  assert.equal(forged.status, 403);
  assert.equal(calls, 0);
  const response = await admin.handleCentralAdminBff("owner-transfer", new Request(`${ORIGIN}/api/admin/agent-cards/card-1/owner-transfers`, { method: "POST", headers: postHeaders, body: JSON.stringify(transfer) }), ORIGIN, "card-1", fetcher);
  assert.equal(response.status, 201);
  assert.equal(calls, 1);
  assert.equal(response.headers.get("set-cookie"), null);
});
