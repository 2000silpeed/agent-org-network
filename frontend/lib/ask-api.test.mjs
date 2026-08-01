import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("./ask-api.ts", import.meta.url), "utf8");
const askPage = await readFile(new URL("../app/ask/page.tsx", import.meta.url), "utf8");
const transpiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  fileName: "ask-api.ts", reportDiagnostics: true,
});
assert.deepEqual(transpiled.diagnostics ?? [], []);
const question = await import(`data:text/javascript;base64,${Buffer.from(transpiled.outputText).toString("base64")}`);

const answered = {
  type: "answered", state: "answered", retryable: false, request_id: "q_01", record_id: "a_01",
  text: "환불은 결제 수단으로 처리됩니다.", answered_by: { owner: "registry-user", agent_id: "refund-card" },
  mode: "full", sources: ["published/refund"], review_status: "approved",
};
const pending = {
  type: "pending", state: "awaiting_approval", kind: "routed", retryable: false,
  request_id: "q_01", message: "답변을 검토하고 있습니다.",
};

function event(type, data, id = "1") {
  return { type, data: JSON.stringify(data), lastEventId: id };
}

test("sealed DTO decoder는 exact lifecycle projection만 받고 caller 또는 내부 필드를 닫는다", () => {
  assert.deepEqual(question.decodeQuestionProjection(answered), answered);
  assert.deepEqual(question.decodeQuestionProjection(pending), pending);
  for (const invalid of [
    { ...answered, owner: "forged" },
    { ...answered, sources: ["published/refund", ""] },
    { ...pending, state: "awaiting_answer", retryable: false },
    { ...pending, route: "internal" },
    { ...answered, mode: "draft_only" },
  ]) assert.equal(question.decodeQuestionProjection(invalid), null);
});

test("create와 feedback은 CSRF·idempotency를 보내고 UTF-8 4096 bytes 및 공백을 보존한다", async () => {
  const seen = [];
  const original = globalThis.fetch;
  globalThis.document = { cookie: "__Host-aon-central-csrf=csrf-value" };
  globalThis.fetch = async (path, init) => {
    seen.push([path, init]);
    if (String(path) === "/api/questions") return new Response(JSON.stringify({ request_id: "q_01", state: "received", created_at: "2026-07-31T00:00:00Z", replayed: false }), { status: 201 });
    return new Response(JSON.stringify({ request_id: "q_01", record_id: "a_01", feedback_id: "f_01", verdict: "good", submitted_at: "2026-07-31T00:00:01Z", replayed: false }), { status: 201 });
  };
  try {
    await question.createQuestion("  원문 보존  ", "create-key");
    await question.submitFeedback("q_01", { record_id: "a_01", verdict: "good", comment: "  고마워요  " }, "feedback-key");
    assert.equal(seen[0][0], "/api/questions");
    assert.equal(seen[0][1].headers["X-AON-CSRF"], "csrf-value");
    assert.equal(seen[0][1].headers["Idempotency-Key"], "create-key");
    assert.equal(seen[0][1].body, JSON.stringify({ question: "  원문 보존  " }));
    assert.equal(seen[1][0], "/api/questions/q_01/feedback");
    assert.equal(seen[1][1].body, JSON.stringify({ record_id: "a_01", verdict: "good", comment: "  고마워요  " }));
    await assert.rejects(question.submitFeedback("q_01", { record_id: "a_01", verdict: "good", comment: "가".repeat(1366) }, "too-large"), /4096/);
  } finally { globalThis.fetch = original; }
});

test("stream event decoder는 event/request/cursor mismatch를 fail-close하고 done은 canonical GET으로만 확정한다", () => {
  assert.deepEqual(question.decodeQuestionStreamEvent(event("pending", pending), "q_01"), { type: "pending", event: pending, cursor: "1" });
  assert.deepEqual(question.decodeQuestionStreamEvent(event("done", answered, "18"), "q_01"), { type: "done", event: answered, cursor: "18" });
  assert.equal(question.decodeQuestionStreamEvent(event("done", { ...answered, request_id: "q_other" }), "q_01"), null);
  assert.equal(question.decodeQuestionStreamEvent(event("token", { request_id: "q_01", text: "draft" }, "0"), "q_01"), null);
  assert.equal(question.decodeQuestionStreamEvent(event("unknown", { request_id: "q_01" }), "q_01"), null);
});

test("canonical GET은 exact request id와 sealed projection을 확인한다", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async (path) => {
    assert.equal(path, "/api/questions/q_01");
    return new Response(JSON.stringify(answered));
  };
  try { assert.deepEqual(await question.retrieveQuestion("q_01"), answered); }
  finally { globalThis.fetch = original; }
});

test("상태 machine은 pending 완료를 주장하지 않고 body-free interruption의 retryability를 구분한다", () => {
  assert.equal(question.lifecycleMessage(pending), "답변을 검토하고 있습니다. 아직 확정되지 않았습니다.");
  assert.equal(question.lifecycleMessage({ type: "interrupted", request_id: "q_01", retryable: false }), "현재 권한으로 질문 상태를 계속 확인할 수 없습니다.");
  assert.equal(question.lifecycleMessage(answered), "답변이 확정되었습니다.");
});

test("exact EventSource는 body-free interrupted를 path에 결박하고 retryable/nonretryable close를 구분한다", () => {
  const listeners = new Map();
  let closed = 0;
  const seen = [];
  const stop = question.subscribeQuestion("q_01", {
    onEvent: (value) => seen.push(value.type),
    onFault: (value) => seen.push(value.retryable ? "retry" : "stop"),
  }, (url) => {
    assert.equal(url, "/api/questions/q_01/stream");
    return { addEventListener: (name, callback) => listeners.set(name, callback), close: () => { closed += 1; } };
  });
  listeners.get("interrupted")(event("interrupted", { request_id: "q_01", retryable: true }, "9"));
  assert.equal(closed, 0);
  listeners.get("done")(event("done", answered, "10"));
  assert.deepEqual(seen, ["interrupted", "done"]);
  assert.equal(closed, 1);
  stop();
  assert.equal(closed, 1);

  let deniedClosed = 0;
  const deniedListeners = new Map();
  question.subscribeQuestion("q_01", { onEvent: (value) => seen.push(value.type), onFault: () => seen.push("fault") }, () => ({ addEventListener: (name, callback) => deniedListeners.set(name, callback), close: () => { deniedClosed += 1; } }));
  deniedListeners.get("interrupted")(event("interrupted", { request_id: "q_01", retryable: false }, "11"));
  assert.equal(deniedClosed, 1);
  assert.equal(question.decodeQuestionStreamEvent(event("interrupted", { request_id: "q_01", retryable: true, message: "leak" }, "12"), "q_01"), null);
  assert.equal(question.decodeQuestionStreamEvent(event("interrupted", { request_id: "q_other", retryable: true }, "12"), "q_01"), null);

  let reconnects = 0;
  let boundedCloses = 0;
  let errorListener;
  question.subscribeQuestion("q_01", { onEvent: () => {}, onFault: () => { reconnects += 100; }, onReconnect: () => { reconnects += 1; } }, () => ({ addEventListener: () => {}, close: () => { boundedCloses += 1; }, set onerror(value) { errorListener = value; } }));
  for (let index = 0; index < 5; index += 1) errorListener(new Event("error"));
  assert.equal(reconnects, 104);
  assert.equal(boundedCloses, 1);
});

test("Next 질문 화면은 Central Session gate와 전용 Question lifecycle만 사용한다", () => {
  assert.match(askPage, /fetch\("\/api\/auth\/session"/);
  assert.match(askPage, /createQuestion\(/);
  assert.match(askPage, /subscribeQuestion\(/);
  assert.match(askPage, /retrieveQuestion\(/);
  assert.match(askPage, /submitFeedback\(/);
  assert.match(askPage, /event\.retryable \? undefined : lifecycleMessage\(event\)/);
  assert.match(askPage, /void converge\(requestId\)/);
  assert.doesNotMatch(askPage, /\/api\/ask|\/api\/requests|streamAsk|postAsk/);
});
