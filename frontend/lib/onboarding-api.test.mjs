import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("./onboarding-api.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 }, fileName: "onboarding-api.ts", reportDiagnostics: true });
assert.deepEqual(compiled.diagnostics ?? [], []);
const api = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);

async function withFetch(fn, run) { const original = globalThis.fetch; globalThis.fetch = fn; try { await run(); } finally { globalThis.fetch = original; } }
function status() { return { revision: 4, card_capability: "available", steps: [{ kind: "user", label: "Registry User", state: "complete" }, { kind: "card", label: "Agent Card", state: "current" }, { kind: "card_owner_installation", label: "Card Owner Installation", state: "locked" }], cards: [], card_owner_installation: { artifact: "agent-org-owner", href: "/onboarding#card-owner-installation" } }; }
function card() { return { agent_id: "support", owner: "root", team: "Support", summary: "Support questions", domains: ["support"], last_reviewed_at: "2026-07-31", maintainer: null, can_answer: [], cannot_answer: [], approval_when: [], collaborate_when: [], knowledge_sources: [], trust_labels: [] }; }

test("status는 exact Card Owner Installation DTO만 허용하고 knowledge alias를 닫는다", async () => {
  await withFetch(async () => new Response(JSON.stringify(status())), async () => {
    const value = await api.getOnboardingStatus(); assert.equal(value.steps[2].kind, "card_owner_installation"); assert.equal(value.card_owner_installation.href, "/onboarding#card-owner-installation");
  });
  const invalid = status(); invalid.steps[2] = { kind: "knowledge", label: "Knowledge", state: "locked" };
  await withFetch(async () => new Response(JSON.stringify(invalid)), async () => {
    await assert.rejects(api.getOnboardingStatus(), (error) => error instanceof api.OnboardingError && error.status === 503);
  });
});

test("safe User/Card 목록은 exact safe projection만 허용한다", async () => {
  await withFetch(async (input) => new Response(JSON.stringify(input === "/api/admin/users" ? [{ user_id: "root", email: "root@example.test", manager: null, sso_link_status: "verified_email_match" }] : [card()])), async () => {
    assert.equal((await api.listRegistryUsers())[0].user_id, "root"); assert.equal((await api.listAgentCards())[0].agent_id, "support");
  });
  await withFetch(async () => new Response(JSON.stringify([{ ...card(), credential: "never" }])), async () => {
    await assert.rejects(api.listAgentCards(), (error) => error instanceof api.OnboardingError && error.status === 503);
  });
});

test("CSRF cookie는 정확히 하나의 bounded opaque token만 허용한다", () => {
  const token = "a".repeat(43); assert.equal(api.readAdmissionCsrfCookie(`other=x; __Host-aon-central-csrf=${token}`), token);
  for (const cookie of ["", "__Host-aon-central-csrf=short", `__Host-aon-central-csrf=${token}; __Host-aon-central-csrf=${token}`, "__Host-aon-central-csrf=%E0%A4%A"]) assert.equal(api.readAdmissionCsrfCookie(cookie), null);
});

test("comma/newline Card list parser는 trim·dedupe하고 local identity를 만들지 않는다", () => {
  assert.deepEqual(api.parseListInput(" legal, hr\nlegal \n "), ["legal", "hr"]); assert.doesNotMatch(source, /localStorage|passwordless|demo identity/i);
});

test("mutation은 CSRF, same-origin credentials, idempotency, actor-free body를 보낸다", async () => {
  const originalDocument = globalThis.document; globalThis.document = { cookie: `__Host-aon-central-csrf=${"a".repeat(43)}` };
  try { await withFetch(async (input, init) => {
    assert.equal(input, "/api/admin/users"); assert.equal(init.credentials, "same-origin"); assert.equal(init.cache, "no-store"); const headers = new Headers(init.headers); assert.equal(headers.get("x-aon-csrf"), "a".repeat(43)); assert.equal(headers.get("idempotency-key"), "request_1"); assert.deepEqual(JSON.parse(init.body), { expected_revision: 1, user_id: "alice", email: "alice@example.test", manager: null });
    return new Response(JSON.stringify({ user_id: "alice", email: "alice@example.test", manager: null, revision: 2, replayed: false }));
  }, async () => { assert.equal((await api.registerRegistryUser({ expected_revision: 1, user_id: "alice", email: "alice@example.test", manager: null }, "request_1")).revision, 2); }); } finally { globalThis.document = originalDocument; }
});

for (const statusCode of [401, 403, 409, 422, 502, 503]) test(`HTTP ${statusCode} is a stable Korean typed admission error`, async () => {
  await withFetch(async () => new Response(JSON.stringify({ error: "secret" }), { status: statusCode }), async () => {
    await assert.rejects(api.getOnboardingStatus(), (error) => error instanceof api.OnboardingError && error.status === statusCode && !error.message.includes("secret"));
  });
});
