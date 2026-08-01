import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile, stat } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

async function loadModule(name) {
  const source = await readFile(new URL(`./${name}.ts`, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    fileName: `${name}.ts`,
    reportDiagnostics: true,
  });
  assert.deepEqual(compiled.diagnostics ?? [], []);
  return import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
}

const runtime = await loadModule("frontend-runtime");
const bff = await loadModule("bff-policy");

test("development은 loopback HTTP 기본 backend만 허용한다", () => {
  const defaultConfig = runtime.readFrontendRuntimeConfig({ AON_FRONTEND_MODE: "development" });
  assert.equal(defaultConfig.ok, true);
  assert.equal(defaultConfig.config.backendUrl.toString(), "http://127.0.0.1:8011/");
  assert.equal(runtime.readFrontendRuntimeConfig({
    AON_FRONTEND_MODE: "development", AON_BACKEND_URL: "http://backend.internal",
  }).ok, false);
});

test("production은 HTTPS public origin과 검증된 backend URL이 없으면 닫힌다", () => {
  for (const env of [
    { AON_FRONTEND_MODE: "production" },
    { AON_FRONTEND_MODE: "production", AON_BACKEND_URL: "https://api.example.test" },
    { AON_FRONTEND_MODE: "production", AON_BACKEND_URL: "https://api.example.test", AON_PUBLIC_ORIGIN: "http://app.example.test" },
  ]) assert.equal(runtime.readFrontendRuntimeConfig(env).ok, false);
  assert.equal(runtime.readFrontendRuntimeConfig({
    AON_FRONTEND_MODE: "production",
    AON_BACKEND_URL: "https://api.example.test/private",
    AON_PUBLIC_ORIGIN: "https://app.example.test",
  }).ok, true);
  assert.equal(runtime.readFrontendRuntimeConfig({
    AON_FRONTEND_MODE: "production",
    AON_BACKEND_URL: "http://developer-api:8011",
    AON_PUBLIC_ORIGIN: "https://app.example.test",
  }).ok, true);
});

test("central-local-reference는 public origin과 loopback Central API를 고정한다", () => {
  const valid = runtime.readFrontendRuntimeConfig({
    AON_FRONTEND_MODE: "central-local-reference", AON_PUBLIC_ORIGIN: "https://central.example.test",
    AON_BACKEND_URL: "http://127.0.0.1:8010",
  });
  assert.equal(valid.ok, true);
  assert.equal(runtime.readFrontendRuntimeConfig({
    AON_FRONTEND_MODE: "central-local-reference", AON_PUBLIC_ORIGIN: "https://central.example.test",
    AON_BACKEND_URL: "http://evil.test:8010",
  }).ok, false);
});

test("generic BFF는 dedicated Central Question lifecycle 경로를 절대 catch하지 않는다", () => {
  assert.equal(bff.isAllowedBffRequest("POST", ["v1", "questions"]), false);
  assert.equal(bff.isAllowedBffRequest("GET", ["v1", "questions", "request-1"]), false);
  assert.equal(bff.isAllowedBffRequest("GET", ["v1", "questions"]), false);
  assert.equal(bff.isAllowedBffRequest("POST", ["v1", "questions", "request-1"]), false);
  for (const [method, path] of [
    ["POST", ["ask"]], ["POST", ["ask", "stream"]],
    ["GET", ["requests", "request-1"]], ["GET", ["monitor"]],
    ["GET", ["org", "graph"]], ["GET", ["manager", "queue"]],
    ["GET", ["onboarding", "status"]], ["GET", ["admin", "users"]],
    ["POST", ["admin", "users"]], ["POST", ["admin", "agent-cards"]],
    ["GET", ["inbox", "cases"]], ["GET", ["inbox", "backup-reviews"]],
    ["GET", ["inbox", "reeval"]], ["GET", ["inbox", "approvals"]],
    ["GET", ["inbox", "approvals", "approval-1"]],
    ["POST", ["cases", "case-1", "concur"]],
    ["POST", ["reeval", "item-1", "review"]],
    ["POST", ["inbox", "approvals", "approval-1", "decide"]],
  ]) {
    assert.equal(bff.isAllowedBffRequest(method, path), false, `${method} /${path.join("/")}`);
  }
  assert.equal(bff.isAllowedBffRequest("GET", ["author", "index", "contract_ops"]), false);
  assert.equal(bff.isAllowedBffRequest("POST", ["author", "run"]), false);
  assert.equal(bff.isAllowedBffRequest("POST", ["author", "publish"]), false);
  assert.equal(bff.isAllowedBffRequest("POST", ["builder", "validate"]), false);
  assert.equal(bff.isAllowedBffRequest("GET", ["builder", "validate"]), false);
  assert.equal(bff.isAllowedBffRequest("POST", ["inbox", "cases", "case-1", "document"]), false);
  assert.equal(bff.isAllowedBffRequest("POST", ["login"]), false);
  assert.equal(bff.isAllowedBffRequest("POST", ["logout"]), false);
  assert.equal(bff.isAllowedBffRequest("DELETE", ["admin", "users"]), false);
  const forwarded = bff.bffRequestHeaders(new Headers({
    accept: "application/json", cookie: "aon_session=ok", authorization: "secret", "x-forwarded-host": "bad",
  }));
  assert.equal(forwarded.get("accept"), "application/json");
  assert.equal(forwarded.get("cookie"), "aon_session=ok");
  assert.equal(forwarded.get("authorization"), null);
  assert.equal(forwarded.get("x-forwarded-host"), null);
});

test("BFF Question Request ID와 upstream path는 path confusion 없이 canonical opaque segment로 닫힌다", () => {
  for (const requestId of [
    "", ".", "..", "...", "/", "request/child", "request\\child", "%2f", "%2F", "%5c",
    "%00", "%0a", "%252f", " request-1", "request-1 ", "request\u0000id", "request\nid",
    "요청-1", "é", "a".repeat(129),
  ]) {
    assert.equal(bff.isAllowedBffRequest("GET", ["v1", "questions", requestId]), false, JSON.stringify(requestId));
  }
  const config = runtime.readFrontendRuntimeConfig({
    AON_FRONTEND_MODE: "central-local-reference",
    AON_PUBLIC_ORIGIN: "https://central.example.test",
    AON_BACKEND_URL: "http://127.0.0.1:8010",
  });
  assert.equal(config.ok, true);
  const target = runtime.upstreamUrl(config.config, ["v1", "questions", "request-1"], "");
  assert.equal(target.pathname, "/v1/questions/request-1");
  for (const segment of ["..", "%2f", "request/child", "request\\child", "요청-1", "request\u0000id"]) {
    assert.throws(() => runtime.upstreamUrl(config.config, ["v1", "questions", segment], ""), RangeError);
  }
});

test("Central Next source는 Card Owner route를 빌드하지 않는다", async () => {
  for (const legacyRoute of ["../app/author/page.tsx", "../app/builder/page.tsx"]) {
    await assert.rejects(stat(new URL(legacyRoute, import.meta.url)));
  }
});

test("Central active route와 navigation은 Owner-local API를 참조하지 않는다", async () => {
  const sources = await Promise.all([
    readFile(new URL("../app/api/[...path]/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../components/app-shell/nav.ts", import.meta.url), "utf8"),
  ]);
  for (const source of sources) {
    assert.doesNotMatch(
      source,
      /owner-api|a2a|builder-api|author-api|\/api\/(?:builder|author)|["']\/(?:builder|author)["']/i,
    );
  }
});

test("Central active entrypoint는 demo identity나 Developer Reference 운영 화면을 빌드하지 않는다", async () => {
  const sources = await Promise.all([
    "../app/layout.tsx",
    "../app/onboarding/page.tsx",
    "../app/inbox/page.tsx",
    "../app/console/page.tsx",
    "../components/app-shell/sidebar.tsx",
    "../components/app-shell/topbar.tsx",
  ].map((path) => readFile(new URL(path, import.meta.url), "utf8")));
  for (const source of sources) {
    assert.doesNotMatch(
      source,
      /components\/session\/(?:identity-switcher|login-gate|session-context)|session-api|IdentitySwitcher|LoginGate|ConsoleView|InboxTabs|DEMO_IDENTITIES|postLogin|aon\.operator\.userId/,
    );
  }
});

test("BFF body limit은 declared와 streamed oversized payload 모두 거부한다", async () => {
  const tooLarge = new Request("http://frontend.test/api/ask", {
    method: "POST", headers: { "content-length": String(bff.MAX_BFF_BODY_BYTES + 1) }, body: "x",
  });
  await assert.rejects(bff.readBffBody(tooLarge), RangeError);
  const body = await bff.readBffBody(new Request("http://frontend.test/api/ask", { method: "POST", body: "ok" }));
  assert.equal(new TextDecoder().decode(body), "ok");
});
