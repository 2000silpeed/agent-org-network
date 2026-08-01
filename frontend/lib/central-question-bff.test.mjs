import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("./central-question-bff.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  fileName: "central-question-bff.ts", reportDiagnostics: true,
});
assert.deepEqual(compiled.diagnostics ?? [], []);
const question = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);

const ORIGIN = "https://central.example.test";
const COOKIE = "__Host-aon-central-session=opaque-session; __Host-aon-central-csrf=csrf";
const CSRF = "a".repeat(43);
const baseHeaders = { Host: "central.example.test", Cookie: COOKIE };
const postHeaders = {
  ...baseHeaders, Origin: ORIGIN, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
  "Sec-Fetch-Dest": "empty", "X-AON-CSRF": CSRF, "Idempotency-Key": "question-1",
  "Content-Type": "application/json",
};

function request(path, init = {}) { return new Request(`${ORIGIN}${path}`, init); }
function createRequest(body = JSON.stringify({ question: "질문" }), headers = postHeaders) {
  return request("/api/questions", { method: "POST", headers, body });
}

test("Question BFF는 exact 네 public route만 fixed Central lifecycle upstream으로 매핑한다", () => {
  assert.equal(question.questionUpstreamUrl("create").toString(), "http://127.0.0.1:8010/v1/questions");
  assert.equal(question.questionUpstreamUrl("retrieve", "request-1").toString(), "http://127.0.0.1:8010/v1/questions/request-1");
  assert.equal(question.questionUpstreamUrl("stream", "request-1").toString(), "http://127.0.0.1:8010/v1/questions/request-1/stream");
  assert.equal(question.questionUpstreamUrl("feedback", "request-1").toString(), "http://127.0.0.1:8010/v1/questions/request-1/feedback");
  assert.throws(() => question.questionUpstreamUrl("retrieve", "request/child"), RangeError);
});

test("Question GET은 cookie-only이고 POST는 exact Origin/Fetch/CSRF/idempotency DTO만 받는다", async () => {
  assert.equal(await question.isValidCentralQuestionRequest("create", createRequest(), ORIGIN), true);
  assert.equal(await question.isValidCentralQuestionRequest("retrieve", request("/api/questions/request-1", { headers: baseHeaders }), ORIGIN, "request-1"), true);
  assert.equal(await question.isValidCentralQuestionRequest("stream", request("/api/questions/request-1/stream", {
    headers: { ...baseHeaders, Accept: "text/event-stream", "Last-Event-ID": "2" },
  }), ORIGIN, "request-1"), true);
  assert.equal(await question.isValidCentralQuestionRequest("create", createRequest(JSON.stringify({ question: "질문" }), {
    ...postHeaders, "X-AON-Admission-Proxy-Provenance": "next-standalone-clean", "X-Forwarded-Host": "central.example.test",
  }), ORIGIN), true);
  assert.equal(await question.isValidCentralQuestionRequest("feedback", request("/api/questions/request-1/feedback", {
    method: "POST", headers: postHeaders, body: JSON.stringify({ record_id: "record-1", verdict: "good", comment: "" }),
  }), ORIGIN, "request-1"), true);
  for (const candidate of [
    createRequest(JSON.stringify({ question: "질문", user: "forged" })),
    createRequest(JSON.stringify({ question: "" })),
    createRequest(JSON.stringify({ question: "질문" }), { ...postHeaders, "Content-Type": "text/plain" }),
    createRequest(JSON.stringify({ question: "질문" }), { ...postHeaders, "Sec-Fetch-Mode": "navigate" }),
    request("/api/questions/request-1?cursor=2", { headers: baseHeaders }),
    request("/api/questions/request-1/stream", { headers: { ...baseHeaders, Accept: "text/event-stream", "Last-Event-ID": "0" } }),
  ]) assert.equal(await question.isValidCentralQuestionRequest("create", candidate, ORIGIN), false);
});

test("forged provenance, self claim, malformed JSON 및 lone surrogate는 backend reach 0으로 닫는다", async () => {
  const badRequests = [
    createRequest(JSON.stringify({ question: "질문" }), { ...postHeaders, Forwarded: "host=evil.example" }),
    createRequest(JSON.stringify({ question: "질문" }), { ...postHeaders, "X-Forwarded-Host": "central.example.test" }),
    createRequest(JSON.stringify({ question: "질문" }), { ...postHeaders, "X-AON-Admission-Proxy-Provenance": "next-standalone-caller-claimed" }),
    createRequest('{"question":"\\ud800"}'),
    createRequest("{not json"),
    createRequest(new Uint8Array([0xff])),
    createRequest(JSON.stringify({ question: "질문" }), { ...postHeaders, "X-AON-Internal-Trace": "forged" }),
    createRequest(JSON.stringify({ question: "질문" }), { ...postHeaders, "Content-Length": "65537" }),
  ];
  for (const candidate of badRequests) {
    let calls = 0;
    const response = await question.handleCentralQuestionBff("create", candidate, ORIGIN, undefined, async () => {
      calls += 1;
      return new Response("unexpected");
    });
    assert.equal(calls, 0);
    assert.ok([403, 422].includes(response.status));
  }
  let wrongMethodCalls = 0;
  const wrongMethod = await question.handleCentralQuestionBff("retrieve", createRequest(), ORIGIN, "request-1", async () => {
    wrongMethodCalls += 1;
    return new Response("unexpected");
  });
  assert.equal(wrongMethod.status, 400);
  assert.equal(wrongMethodCalls, 0);
});

test("relay는 allowlisted status/body만 Korean safe DTO로 만들고 cookie/header/raw body를 버린다", async () => {
  let calls = 0;
  const created = await question.handleCentralQuestionBff("create", createRequest(), ORIGIN, undefined, async (url, init) => {
    calls += 1;
    assert.equal(url.toString(), "http://127.0.0.1:8010/v1/questions");
    assert.equal(init.headers.get("authorization"), null);
    assert.equal(init.headers.get("cookie"), COOKIE);
    return new Response(JSON.stringify({ request_id: "request-1", state: "received", created_at: "2026-07-31T00:00:00Z", replayed: false }), {
      status: 201, headers: { "content-type": "application/json", "set-cookie": "leak=1", "x-leak": "no" },
    });
  });
  assert.equal(calls, 1);
  assert.equal(created.status, 201);
  assert.equal(created.headers.get("set-cookie"), null);
  assert.equal(created.headers.get("x-leak"), null);
  assert.deepEqual(await created.json(), { request_id: "request-1", state: "received", created_at: "2026-07-31T00:00:00Z", replayed: false });

  const denied = await question.relayCentralQuestionResponse("retrieve", "request-1", new Response('{"error":"question_not_found"}', { status: 404 }));
  assert.equal(denied.status, 404);
  assert.deepEqual(await denied.json(), { error: "question_not_found", message: "질문 요청을 찾을 수 없습니다." });
  const sessionForbidden = await question.relayCentralQuestionResponse("retrieve", "request-1", new Response('{"error":"browser_session_forbidden"}', {
    status: 403, headers: { "set-cookie": "leak=1", "x-leak": "no" },
  }));
  assert.equal(sessionForbidden.status, 403);
  assert.equal(sessionForbidden.headers.get("set-cookie"), null);
  assert.equal(sessionForbidden.headers.get("x-leak"), null);
  assert.deepEqual(await sessionForbidden.json(), {
    error: "browser_session_forbidden", message: "현재 브라우저 세션으로 이 요청을 처리할 수 없습니다.",
  });
  const unknownForbidden = await question.relayCentralQuestionResponse("retrieve", "request-1", new Response('{"error":"other_forbidden"}', { status: 403 }));
  assert.equal(unknownForbidden.status, 502);
  assert.doesNotMatch(await unknownForbidden.text(), /other_forbidden/);
  const malformed = await question.relayCentralQuestionResponse("retrieve", "request-1", new Response("raw upstream secret", { status: 500 }));
  assert.equal(malformed.status, 502);
  assert.doesNotMatch(await malformed.text(), /secret/);
});

test("SSE는 bytes/event boundary를 buffer 없이 통과시키고 browser abort를 upstream fetch에 연결한다", async () => {
  const streamRequest = request("/api/questions/request-1/stream", {
    headers: { ...baseHeaders, Accept: "text/event-stream" },
  });
  const source = "id: 1\nevent: accepted\ndata: {\"request_id\":\"request-1\"}\n\n";
  const stream = await question.handleCentralQuestionBff("stream", streamRequest, ORIGIN, "request-1", async (_url, init) => {
    assert.equal(init.headers.get("accept"), "text/event-stream");
    assert.equal(init.headers.get("last-event-id"), null);
    return new Response(source, { status: 200, headers: { "content-type": "text/event-stream", "set-cookie": "leak=1" } });
  });
  assert.equal(stream.headers.get("cache-control"), "no-cache, no-transform");
  assert.equal(stream.headers.get("set-cookie"), null);
  assert.equal(await stream.text(), source);

  const controller = new AbortController();
  controller.abort();
  const abortRequest = request("/api/questions/request-1/stream", {
    headers: { ...baseHeaders, Accept: "text/event-stream" }, signal: controller.signal,
  });
  let aborted = false;
  const interrupted = await question.handleCentralQuestionBff("stream", abortRequest, ORIGIN, "request-1", async (_url, init) => {
    aborted = init.signal.aborted;
    throw new Error("aborted");
  });
  assert.equal(interrupted.status, 502);
  assert.equal(aborted, true);
});

test("dedicated route source와 standalone wrapper는 generic Question fallback을 만들지 않는다", async () => {
  const [generic, policy, create, retrieve, stream, feedback, assembly] = await Promise.all([
    readFile(new URL("../app/api/[...path]/route.ts", import.meta.url), "utf8"),
    readFile(new URL("./bff-policy.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/questions/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/questions/[request_id]/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/questions/[request_id]/stream/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/questions/[request_id]/feedback/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../scripts/assemble-standalone.mjs", import.meta.url), "utf8"),
  ]);
  assert.match(policy, /return false/);
  assert.doesNotMatch(generic, /v1\/questions/);
  assert.match(create, /"create"/);
  assert.match(retrieve, /"retrieve"/);
  assert.match(stream, /"stream"/);
  assert.match(feedback, /"feedback"/);
  assert.match(assembly, /standalone-admission-provenance-guard/);
});
