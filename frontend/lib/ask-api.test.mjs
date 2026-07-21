import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { Buffer } from "node:buffer";
import ts from "typescript";

const source = await readFile(new URL("./ask-api.ts", import.meta.url), "utf8");
const transpiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
  fileName: "ask-api.ts",
  reportDiagnostics: true,
});
assert.deepEqual(transpiled.diagnostics ?? [], []);

const {
  AskError,
  getRequest,
  parseAskSseFrame,
  parseOrgReply,
  parseRequestResult,
  streamAsk,
} = await import(`data:text/javascript;base64,${Buffer.from(transpiled.outputText).toString("base64")}`);

const answeredLegacy = {
  type: "answered",
  request_id: "request-1",
  record_id: "record-1",
  text: "환불 답변",
  answered_by: { owner: "owner-1", agent_id: "refund-card" },
  mode: "full",
  sources: ["refund-policy.md"],
  review_status: "not_required",
};

const answeredNative = {
  answer_text: "환불 답변",
  request_id: "request-1",
  record_id: "record-1",
  mode: "full",
  sources: ["refund-policy.md"],
  review_status: "not_required",
  answered_by: "owner-1",
  agent_id: "refund-card",
};

function sse(event, data) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function streamResponse(body, requestId = "request-1") {
  let cancelled = false;
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(body));
    },
    cancel() {
      cancelled = true;
    },
  });
  return {
    response: new Response(stream, { headers: { "X-Request-ID": requestId } }),
    stream,
    wasCancelled: () => cancelled,
  };
}

async function withFetch(response, run) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => response;
  try {
    await run();
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test("legacy Pending은 tracking exact alias만 받고 공개 결과에서는 버린다", () => {
  const pending = {
    type: "pending",
    request_id: "request-1",
    kind: "dispatched",
    state: "awaiting_answer",
    retryable: true,
    message: "처리 중",
    tracking: "request-1",
  };

  assert.deepEqual(parseOrgReply(pending), {
    type: "pending",
    request_id: "request-1",
    kind: "dispatched",
    state: "awaiting_answer",
    retryable: true,
    message: "처리 중",
  });
  assert.equal(parseOrgReply({ ...pending, tracking: "bearer-token" }), null);
  assert.deepEqual(parseOrgReply(answeredLegacy), answeredLegacy);
});

test("native lookup은 legacy type·tracking·text를 거부하고 안전한 결과로 정규화한다", () => {
  assert.deepEqual(parseRequestResult(answeredNative), answeredLegacy);
  for (const field of ["type", "tracking", "text"]) {
    assert.equal(parseRequestResult({ ...answeredNative, [field]: "legacy" }), null);
  }
  assert.equal(parseRequestResult({ ...answeredNative, candidates: [] }), null);
});

test("P17 SSE parser는 공개 이벤트만 읽고 내부 필드를 거부한다", () => {
  const request_id = "request-1";
  const events = [
    sse("accepted", { request_id }),
    sse("token", { request_id, text: "답" }),
    sse("pending", {
      request_id,
      kind: "routed",
      state: "awaiting_answer",
      retryable: true,
      message: "처리 중",
    }),
    sse("done", {
      request_id,
      record_id: "record-1",
      mode: "full",
      sources: ["refund-policy.md"],
      review_status: "not_required",
      answered_by: "owner-1",
      agent_id: "refund-card",
    }),
    sse("declined", { request_id, reason_code: "declined", message: "거절" }),
    sse("failed", { request_id, error_code: "failed", message: "실패" }),
    sse("interrupted", { request_id, retryable: true, message: "중단" }),
  ];

  assert.deepEqual(
    events.map((frame) => parseAskSseFrame(frame)?.type),
    ["accepted", "token", "pending", "done", "declined", "failed", "interrupted"]
  );
  assert.equal(
    parseAskSseFrame(sse("accepted", { request_id, policy: "internal" })),
    null
  );
  assert.equal(parseAskSseFrame(sse("meta", { request_id })), null);
});

test("stream은 모든 request_id를 묶고 종착 뒤 reader를 취소·해제한다", async () => {
  const body =
    sse("accepted", { request_id: "request-1" }) +
    sse("done", {
      request_id: "request-1",
      record_id: "record-1",
      mode: "full",
      sources: [],
      review_status: "not_required",
      answered_by: "owner-1",
      agent_id: "refund-card",
    });
  const fixture = streamResponse(body);
  const seen = [];

  await withFetch(fixture.response, async () => {
    await streamAsk("환불은 언제 되나요?", {
      onAccepted: (event) => seen.push(event.request_id),
      onDone: (event) => seen.push(event.record_id),
    });
  });

  assert.deepEqual(seen, ["request-1", "record-1"]);
  assert.equal(fixture.wasCancelled(), true);
  assert.equal(fixture.stream.locked, false);
});

test("stream ID 불일치·종착 뒤 추가 프레임은 거부하고 reader를 정리한다", async () => {
  const cases = [
    sse("accepted", { request_id: "request-1" }) +
      sse("token", { request_id: "request-2", text: "누수" }),
    sse("accepted", { request_id: "request-1" }) +
      sse("failed", { request_id: "request-1", error_code: "failed", message: "실패" }) +
      sse("token", { request_id: "request-1", text: "늦은 데이터" }),
  ];

  for (const body of cases) {
    const fixture = streamResponse(body);
    await withFetch(fixture.response, async () => {
      await assert.rejects(streamAsk("질문", {}), AskError);
    });
    assert.equal(fixture.wasCancelled(), true);
    assert.equal(fixture.stream.locked, false);
  }
});

test("handler 예외에도 reader를 정리한다", async () => {
  const fixture = streamResponse(sse("accepted", { request_id: "request-1" }));
  await withFetch(fixture.response, async () => {
    await assert.rejects(
      streamAsk("질문", {
        onAccepted: () => {
          throw new Error("handler failed");
        },
      }),
      /handler failed/
    );
  });
  assert.equal(fixture.wasCancelled(), true);
  assert.equal(fixture.stream.locked, false);
});

test("canonical GET은 native URI와 header·body request_id를 함께 검증한다", async () => {
  let requested = "";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    requested = String(input);
    return new Response(JSON.stringify(answeredNative), {
      headers: { "content-type": "application/json", "X-Request-ID": "request-1" },
    });
  };
  try {
    const result = await getRequest("request-1");
    assert.equal(requested, "/api/requests/request-1");
    assert.deepEqual(result, answeredLegacy);
  } finally {
    globalThis.fetch = originalFetch;
  }

  await withFetch(
    new Response(JSON.stringify(answeredNative), {
      headers: { "content-type": "application/json", "X-Request-ID": "request-other" },
    }),
    async () => {
      await assert.rejects(getRequest("request-1"), /요청 ID가 다른 응답/);
    }
  );
});
