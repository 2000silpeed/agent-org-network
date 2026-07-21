import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("./inbox-api.ts", import.meta.url), "utf8");
const transpiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
  fileName: "inbox-api.ts",
  reportDiagnostics: true,
});
assert.deepEqual(transpiled.diagnostics ?? [], []);

const {
  getApprovalDetail,
  getInboxApprovals,
  postApprovalDecision,
  postApprovalReassignment,
  postConcur,
} = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.outputText).toString("base64")}`
);

test("postConcur는 서버 current round와 stance를 exact body로 전송한다", async () => {
  const originalFetch = globalThis.fetch;
  let requestUrl = "";
  let requestInit;
  globalThis.fetch = async (input, init) => {
    requestUrl = String(input);
    requestInit = init;
    return new Response(
      JSON.stringify({
        type: "still_open",
        request_id: "request-1",
        case_id: "case-1",
        current_round: 3,
        pending_owners: ["finance_lead"],
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
  try {
    await postConcur("case-1", "cs_ops", 3, "keep_as_complement", "근거");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(requestUrl, "/api/cases/case-1/concur");
  assert.equal(requestInit?.method, "POST");
  assert.deepEqual(JSON.parse(String(requestInit?.body)), {
    on_agent: "cs_ops",
    rationale: "근거",
    expected_round: 3,
    stance: "keep_as_complement",
  });
});

test("두 UI는 받은 current_round를 그대로 보내고 로컬 증가시키지 않는다", async () => {
  const staticUi = await readFile(
    new URL("../../web/inbox.html", import.meta.url),
    "utf8",
  );
  const nextUi = await readFile(
    new URL("../components/inbox/inbox-tabs.tsx", import.meta.url),
    "utf8",
  );

  for (const ui of [staticUi, nextUi]) {
    assert.match(ui, /current_round/);
    assert.match(ui, /keep_as_complement/);
    assert.doesNotMatch(ui, /current_round\s*\+\s*1/);
  }
});

test("두 UI는 keep_as_complement를 내 후보의 보조 지식 유지로 설명한다", async () => {
  const staticUi = await readFile(
    new URL("../../web/inbox.html", import.meta.url),
    "utf8",
  );
  const nextUi = await readFile(
    new URL("../components/inbox/inbox-tabs.tsx", import.meta.url),
    "utf8",
  );
  const expected = "내 후보가 primary가 아니면 내 후보 지식을 보조 근거로 유지";

  for (const ui of [staticUi, nextUi]) {
    assert.match(ui, new RegExp(expected));
    assert.doesNotMatch(ui, /선택한 후보의 지식을 보조 근거로 유지/);
  }
});

test("Approval API는 Next proxy의 queue와 detail 경로를 구분한다", async () => {
  const originalFetch = globalThis.fetch;
  const urls = [];
  globalThis.fetch = async (input) => {
    urls.push(String(input));
    return new Response(JSON.stringify([]), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    await getInboxApprovals();
    await getApprovalDetail("approval/한글");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.deepEqual(urls, [
    "/api/inbox/approvals",
    "/api/inbox/approvals/approval%2F%ED%95%9C%EA%B8%80",
  ]);
});

test("Approval 처분은 kind별 exact body만 보내고 actor나 org를 싣지 않는다", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (input, init) => {
    requests.push({ url: String(input), init });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    await postApprovalDecision("approval-1", { kind: "approve" });
    await postApprovalDecision("approval-1", {
      kind: "approve_with_edit",
      edited_text: "수정 답변",
    });
    await postApprovalDecision("approval-1", {
      kind: "reject",
      reason_code: "unsupported",
    });
    await postApprovalReassignment("approval-1", "next-owner");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.deepEqual(
    requests.map(({ url, init }) => ({
      url,
      method: init?.method,
      body: JSON.parse(String(init?.body)),
    })),
    [
      {
        url: "/api/inbox/approvals/approval-1/decide",
        method: "POST",
        body: { kind: "approve" },
      },
      {
        url: "/api/inbox/approvals/approval-1/decide",
        method: "POST",
        body: { kind: "approve_with_edit", edited_text: "수정 답변" },
      },
      {
        url: "/api/inbox/approvals/approval-1/decide",
        method: "POST",
        body: { kind: "reject", reason_code: "unsupported" },
      },
      {
        url: "/api/inbox/approvals/approval-1/reassign",
        method: "POST",
        body: { approver_id: "next-owner" },
      },
    ],
  );
  for (const request of requests) {
    const body = JSON.parse(String(request.init?.body));
    assert.equal("actor_id" in body, false);
    assert.equal("org_id" in body, false);
    assert.equal("principal" in body, false);
  }
});

test("두 처리함은 네 번째 Approval 탭과 lazy detail·필수 입력 계약을 가진다", async () => {
  const staticUi = await readFile(
    new URL("../../web/inbox.html", import.meta.url),
    "utf8",
  );
  const nextUi = await readFile(
    new URL("../components/inbox/inbox-tabs.tsx", import.meta.url),
    "utf8",
  );

  for (const ui of [staticUi, nextUi]) {
    assert.match(ui, /Approval/);
    assert.match(ui, /inbox\/approvals|getInboxApprovals/);
    assert.match(ui, /approve_with_edit/);
    assert.match(ui, /reason_code/);
    assert.match(ui, /approver_id|approverId/);
    assert.match(ui, /role=["']tabpanel["']/);
    assert.doesNotMatch(ui, /actor_id\s*:/);
    assert.doesNotMatch(ui, /org_id\s*:/);
  }
  assert.match(staticUi, /textContent/);
  assert.match(nextUi, /getApprovalDetail/);
});

test("Approval 상세는 마지막 선택 요청만 적용하고 이전 상세를 처분 대상으로 남기지 않는다", async () => {
  const staticUi = await readFile(
    new URL("../../web/inbox.html", import.meta.url),
    "utf8",
  );
  const nextUi = await readFile(
    new URL("../components/inbox/inbox-tabs.tsx", import.meta.url),
    "utf8",
  );

  const nextDetail = nextUi.slice(
    nextUi.indexOf("async function openDetail"),
    nextUi.indexOf("async function act", nextUi.indexOf("async function openDetail")),
  );
  assert.match(nextUi, /detailRequestEpoch/);
  assert.match(nextDetail, /\+\+detailRequestEpoch\.current/);
  assert.ok(
    nextDetail.indexOf("detailRequest !== detailRequestEpoch.current") <
      nextDetail.indexOf("setDetail(loaded)"),
  );

  const staticDetailStart = staticUi.indexOf('openBtn.addEventListener("click"');
  const staticDetail = staticUi.slice(
    staticDetailStart,
    staticUi.indexOf("return wrap", staticDetailStart),
  );
  assert.match(staticUi, /approvalDetailRequest/);
  assert.match(staticUi, /activeApprovalDetailClose/);
  assert.ok(
    staticDetail.indexOf("detailRequest !== approvalDetailRequest") <
      staticDetail.indexOf('detailBox.appendChild(el("div", "question"'),
  );
});

test("세션 또는 load epoch 변경은 Approval 상태를 비우고 이전 응답을 봉인한다", async () => {
  const staticUi = await readFile(
    new URL("../../web/inbox.html", import.meta.url),
    "utf8",
  );
  const nextUi = await readFile(
    new URL("../components/inbox/inbox-tabs.tsx", import.meta.url),
    "utf8",
  );

  const nextRefresh = nextUi.slice(
    nextUi.indexOf("const refresh = useCallback"),
    nextUi.indexOf("useEffect", nextUi.indexOf("const refresh = useCallback")),
  );
  assert.match(nextUi, /refreshEpoch/);
  assert.ok(
    nextRefresh.indexOf("refreshRequest !== refreshEpoch.current") <
      nextRefresh.indexOf("setApprovals(ap)"),
  );
  assert.match(nextUi, /setApprovals\(\[\]\)/);
  assert.match(
    nextUi,
    /const visibleApprovals = approvalSession === userId \? approvals : \[\]/,
  );
  assert.match(nextUi, /approvals=\{visibleApprovals\}/);
  assert.match(nextUi, /key=\{userId/);

  const staticLoad = staticUi.slice(
    staticUi.indexOf("async function loadApprovals"),
    staticUi.indexOf("async function loadReviews"),
  );
  assert.match(staticUi, /approvalLoadEpoch/);
  assert.match(staticUi, /beginApprovalLoadGeneration/);
  assert.ok(
    staticLoad.indexOf("generation !== approvalLoadEpoch") <
      staticLoad.indexOf("approvalCountEl.textContent"),
  );
  for (const functionName of ["showLogin", "showIdentity"]) {
    const start = staticUi.indexOf(`function ${functionName}`);
    const end = staticUi.indexOf("\n    }", start);
    assert.match(staticUi.slice(start, end), /beginApprovalLoadGeneration\(\)/);
  }
});

test("정적 UI의 새 세션과 load 세대는 Approval 카드와 count를 즉시 0으로 만든다", async () => {
  const staticUi = await readFile(
    new URL("../../web/inbox.html", import.meta.url),
    "utf8",
  );
  const generationStart = staticUi.indexOf("function beginApprovalLoadGeneration");
  const generation = staticUi.slice(
    generationStart,
    staticUi.indexOf("// ── 탭 전환", generationStart),
  );

  assert.match(generation, /approvalsEl\.textContent = ""/);
  assert.match(generation, /approvalCountEl\.textContent = "0"/);
  assert.match(generation, /approvalCountEl\.classList\.add\("zero"\)/);
  assert.ok(
    generation.indexOf('approvalCountEl.textContent = "0"') <
      generation.indexOf("return approvalLoadEpoch"),
  );

  const loadStart = staticUi.indexOf("async function loadApprovals");
  const loadApprovals = staticUi.slice(
    loadStart,
    staticUi.indexOf("async function loadReviews", loadStart),
  );
  assert.ok(
    loadApprovals.indexOf("generation !== approvalLoadEpoch") <
      loadApprovals.indexOf("approvalCountEl.textContent = String(items.length)"),
  );
});
