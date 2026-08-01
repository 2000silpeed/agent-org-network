import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("./central-inbox-bff.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  fileName: "central-inbox-bff.ts", reportDiagnostics: true,
});
assert.deepEqual(compiled.diagnostics ?? [], []);
const inbox = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);

const ORIGIN = "https://central.example.test";
const COOKIE = "__Host-aon-central-session=opaque-session; __Host-aon-central-csrf=csrf";
const CSRF = "a".repeat(43);
const getHeaders = { Host: "central.example.test", Cookie: COOKIE };
const postHeaders = {
  ...getHeaders, Origin: ORIGIN, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
  "Sec-Fetch-Dest": "empty", "X-AON-CSRF": CSRF, "Idempotency-Key": "inbox-command-1",
  "Content-Type": "application/json",
};

const cases = [
  ["conflict-list", undefined, "/api/inbox/conflicts", "/v1/inbox/conflicts"],
  ["conflict-detail", "case-1", "/api/inbox/conflicts/case-1", "/v1/inbox/conflicts/case-1"],
  ["conflict-concurrence", "case-1", "/api/inbox/conflicts/case-1/concurrences", "/v1/inbox/conflicts/case-1/concurrences"],
  ["backup-list", undefined, "/api/inbox/backup-reviews", "/v1/inbox/backup-reviews"],
  ["backup-detail", "review-1", "/api/inbox/backup-reviews/review-1", "/v1/inbox/backup-reviews/review-1"],
  ["backup-disposition", "review-1", "/api/inbox/backup-reviews/review-1/dispositions", "/v1/inbox/backup-reviews/review-1/dispositions"],
  ["reevaluation-list", undefined, "/api/inbox/reevaluations", "/v1/inbox/reevaluations"],
  ["reevaluation-detail", "reeval-1", "/api/inbox/reevaluations/reeval-1", "/v1/inbox/reevaluations/reeval-1"],
  ["reevaluation-disposition", "reeval-1", "/api/inbox/reevaluations/reeval-1/dispositions", "/v1/inbox/reevaluations/reeval-1/dispositions"],
  ["approval-list", undefined, "/api/inbox/approvals", "/v1/inbox/approvals"],
  ["approval-detail", "approval-1", "/api/inbox/approvals/approval-1", "/v1/inbox/approvals/approval-1"],
  ["approval-disposition", "approval-1", "/api/inbox/approvals/approval-1/dispositions", "/v1/inbox/approvals/approval-1/dispositions"],
  ["approval-reassignment", "approval-1", "/api/inbox/approvals/approval-1/reassignments", "/v1/inbox/approvals/approval-1/reassignments"],
];

const bodies = {
  "conflict-concurrence": {
    on_candidate_card_id: "card-1", stance: "withdraw", rationale: "not primary",
    expected_case_revision: 1, expected_request_revision: 2, expected_round: 1,
  },
  "backup-disposition": { kind: "correct", corrected_text: "new answer", rationale: "fix", expected_revision: 1 },
  "reevaluation-disposition": { kind: "request_reanswer", rationale: "retry", expected_revision: 1 },
  "approval-disposition": { kind: "reject", reason_code: "incorrect", expected_approval_item_revision: 1, expected_request_revision: 2 },
  "approval-reassignment": {
    target_approver_user_id: "user-2", target_approval_card_id: "card-2",
    expected_approval_item_revision: 1, expected_request_revision: 2,
  },
};

test("Inbox BFF는 13개 public route를 exact fixed loopback private route로만 매핑한다", () => {
  for (const [route, id, , privatePath] of cases) {
    assert.equal(inbox.inboxUpstreamUrl(route, id).toString(), `http://127.0.0.1:8010${privatePath}`);
  }
  assert.throws(() => inbox.inboxUpstreamUrl("approval-detail", "item/child"), RangeError);
  assert.throws(() => inbox.inboxUpstreamUrl("approval-list", "caller-selected"), RangeError);
});

test("GET cookie-only, POST same-origin CSRF/idempotency/exact DTO 계약을 강제한다", async () => {
  for (const [route, id, publicPath] of cases) {
    const method = route.endsWith("-list") || route.endsWith("-detail") ? "GET" : "POST";
    const request = new Request(`${ORIGIN}${publicPath}`, method === "GET"
      ? { headers: getHeaders }
      : { method, headers: postHeaders, body: JSON.stringify(bodies[route]) });
    assert.equal(await inbox.isValidCentralInboxRequest(route, request, ORIGIN, id), true, route);
  }
  const invalid = [
    new Request(`${ORIGIN}/api/inbox/conflicts?all=1`, { headers: getHeaders }),
    new Request(`${ORIGIN}/api/inbox/conflicts`, { headers: { ...getHeaders, "X-AON-User": "forged" } }),
    new Request(`${ORIGIN}/api/inbox/approvals/approval-1/dispositions`, {
      method: "POST", headers: postHeaders,
      body: JSON.stringify({ ...bodies["approval-disposition"], actor: "forged" }),
    }),
    new Request(`${ORIGIN}/api/inbox/backup-reviews/review-1/dispositions`, {
      method: "POST", headers: postHeaders,
      body: '{"kind":"approve","rationale":"\\ud800","expected_revision":1}',
    }),
  ];
  assert.equal(await inbox.isValidCentralInboxRequest("conflict-list", invalid[0], ORIGIN), false);
  assert.equal(await inbox.isValidCentralInboxRequest("conflict-list", invalid[1], ORIGIN), false);
  assert.equal(await inbox.isValidCentralInboxRequest("approval-disposition", invalid[2], ORIGIN, "approval-1"), false);
  assert.equal(await inbox.isValidCentralInboxRequest("backup-disposition", invalid[3], ORIGIN, "review-1"), false);
});

test("forged provenance와 malformed body는 backend reach 0으로 닫는다", async () => {
  let calls = 0;
  const fetcher = async () => { calls += 1; return new Response(); };
  const forged = [
    { ...getHeaders, "X-AON-Admission-Proxy-Provenance": "next-standalone-clean" },
    { ...getHeaders, "X-Forwarded-Host": "central.example.test" },
    {
      ...getHeaders,
      "X-AON-Admission-Proxy-Provenance": "next-standalone-clean",
      "X-Forwarded-Host": "central.example.test",
      "X-Forwarded-Proto": "https",
      "X-Forwarded-For": "127.0.0.1",
    },
  ];
  for (const headers of forged) {
    const response = await inbox.handleCentralInboxBff(
      "approval-list",
      new Request(`${ORIGIN}/api/inbox/approvals`, { headers }),
      ORIGIN,
      undefined,
      fetcher,
    );
    assert.equal(response.status, 403);
    assert.deepEqual(await response.json(), {
      code: "invalid_input", message: "처리함 요청 형식이 올바르지 않습니다.",
    });
  }
  assert.equal(calls, 0);
});

test("wrapper-only companion proof가 있을 때만 synthesized forwarding을 허용한다", async () => {
  const symbol = Symbol.for("agent-org-network.standalone-provenance-proof");
  Reflect.set(globalThis, symbol, "process-secret-proof");
  try {
    const request = new Request(`${ORIGIN}/api/inbox/approvals`, {
      headers: {
        ...getHeaders,
        "X-AON-Admission-Proxy-Provenance": "next-standalone-clean",
        "AON-Standalone-Provenance-Proof": "process-secret-proof",
        "X-Forwarded-Host": "central.example.test",
        "X-Forwarded-Port": "443",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "127.0.0.1",
      },
    });
    assert.equal(await inbox.isValidCentralInboxRequest("approval-list", request, ORIGIN), true);
  } finally {
    Reflect.deleteProperty(globalThis, symbol);
  }
});

test("success와 허용된 오류만 exact projection으로 재직렬화한다", async () => {
  const request = new Request(`${ORIGIN}/api/inbox/approvals`, { headers: getHeaders });
  const ok = await inbox.handleCentralInboxBff("approval-list", request, ORIGIN, undefined, async (url, init) => {
    assert.equal(url.toString(), "http://127.0.0.1:8010/v1/inbox/approvals");
    assert.equal(init.headers.get("cookie"), COOKIE);
    return new Response(JSON.stringify({ items: [] }), {
      status: 200, headers: { "content-type": "application/json", "set-cookie": "unsafe=1" },
    });
  });
  assert.equal(ok.status, 200);
  assert.deepEqual(await ok.json(), { items: [] });
  assert.equal(ok.headers.get("set-cookie"), null);

  const approval = {
    approval_item_id: "approval-1",
    request_id: "request-1",
    request_revision: 3,
    approval_round: 1,
    revision: 1,
    assigned_at: "2026-07-31T00:00:00Z",
    due_at: "2026-07-31T00:05:00Z",
    state: "open",
  };
  const list = await inbox.relayCentralInboxResponse("approval-list", new Response(
    JSON.stringify({ items: [approval] }),
    { status: 200, headers: { "content-type": "application/json" } },
  ));
  assert.equal(list.status, 200);
  assert.deepEqual(await list.json(), { items: [approval] });

  const detailValue = {
    ...approval,
    question: "approve?",
    candidate_text: "candidate",
    candidate_digest: "a".repeat(64),
    policy_digest: "b".repeat(64),
    binding_version: 1,
    assigned_approver_user_id: "user-1",
    assigned_approval_card_id: "card-1",
  };
  const detail = await inbox.relayCentralInboxResponse("approval-detail", new Response(
    JSON.stringify(detailValue),
    { status: 200, headers: { "content-type": "application/json" } },
  ));
  assert.equal(detail.status, 200);
  assert.deepEqual(await detail.json(), detailValue);

  const invalidRevisions = [
    ["missing", undefined],
    ["string", "3"],
    ["zero", 0],
    ["negative", -1],
  ];
  for (const [label, revision] of invalidRevisions) {
    const invalidSummary = { ...approval, request_revision: revision };
    const invalidDetail = {
      ...detailValue,
      request_revision: revision,
      question: "unsafe-upstream-secret",
    };
    if (label === "missing") {
      delete invalidSummary.request_revision;
      delete invalidDetail.request_revision;
    }

    const unsafeList = await inbox.relayCentralInboxResponse("approval-list", new Response(
      JSON.stringify({ items: [invalidSummary] }),
      {
        status: 200,
        headers: {
          "content-type": "application/json",
          "set-cookie": "unsafe=1",
          "x-internal-trace": "secret",
        },
      },
    ));
    assert.equal(unsafeList.status, 502, `approval list ${label}`);
    assert.deepEqual(await unsafeList.json(), {
      code: "unavailable", message: "처리함 서비스를 지금 사용할 수 없습니다.",
    });
    assert.equal(unsafeList.headers.get("set-cookie"), null);
    assert.equal(unsafeList.headers.get("x-internal-trace"), null);

    const unsafeDetail = await inbox.relayCentralInboxResponse("approval-detail", new Response(
      JSON.stringify(invalidDetail),
      {
        status: 200,
        headers: {
          "content-type": "application/json",
          "set-cookie": "unsafe=1",
          "x-internal-trace": "secret",
        },
      },
    ));
    assert.equal(unsafeDetail.status, 502, `approval detail ${label}`);
    assert.deepEqual(await unsafeDetail.json(), {
      code: "unavailable", message: "처리함 서비스를 지금 사용할 수 없습니다.",
    });
    assert.equal(unsafeDetail.headers.get("set-cookie"), null);
    assert.equal(unsafeDetail.headers.get("x-internal-trace"), null);
  }

  const denied = await inbox.relayCentralInboxResponse("approval-detail", new Response(
    JSON.stringify({ error: "not_found_or_denied" }),
    { status: 404, headers: { "content-type": "application/json" } },
  ));
  assert.equal(denied.status, 404);
  assert.deepEqual(await denied.json(), {
    code: "not_found_or_denied", message: "처리함 항목을 찾을 수 없습니다.",
  });

  const stale = await inbox.relayCentralInboxResponse("approval-disposition", new Response(
    JSON.stringify({ error: "stale_or_conflict" }),
    { status: 409, headers: { "content-type": "application/json" } },
  ));
  assert.equal(stale.status, 409);
  assert.deepEqual(await stale.json(), {
    code: "stale_or_conflict", message: "항목이 변경되었습니다. 새로 고친 뒤 다시 시도해 주세요.",
  });

  const unsafe = await inbox.relayCentralInboxResponse("approval-detail", new Response(
    JSON.stringify({ error: "database_trace", detail: "secret" }),
    { status: 503, headers: { "content-type": "application/json" } },
  ));
  assert.equal(unsafe.status, 502);
  assert.deepEqual(await unsafe.json(), {
    code: "unavailable", message: "처리함 서비스를 지금 사용할 수 없습니다.",
  });
});
