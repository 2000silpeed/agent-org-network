import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("./inbox-api.ts", import.meta.url), "utf8");
const ui = await readFile(new URL("../components/inbox/inbox-tabs.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 }, fileName: "inbox-api.ts", reportDiagnostics: true });
assert.deepEqual(compiled.diagnostics ?? [], []);
const inbox = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);

const sha = "a".repeat(64);
const conflict = { case_id: "case-1", request_id: "request-1", request_revision: 2, state: "open", round: 1, revision: 1, candidate_card_ids: ["card-1"], opened_at: "2026-07-31T00:00:00Z" };
const backup = { review_id: "review-1", request_id: "request-1", source_answer_record_id: "answer-1", revision: 1, state: "open", created_at: "2026-07-31T00:00:00Z" };
const reevaluation = { reevaluation_id: "reeval-1", request_id: "request-1", feedback_id: "feedback-1", source_answer_record_id: "answer-1", revision: 1, state: "open", created_at: "2026-07-31T00:00:00Z" };
const approval = { approval_item_id: "approval-1", request_id: "request-1", request_revision: 2, approval_round: 1, revision: 1, assigned_at: "2026-07-31T00:00:00Z", due_at: "2026-08-01T00:00:00Z", state: "open" };

test("네 목록 decoder는 exact metadata만 받고 raw/internal 필드를 닫는다", () => {
  assert.deepEqual(inbox.decodeConflictList({ items: [conflict] }), [conflict]);
  assert.deepEqual(inbox.decodeBackupList({ items: [backup] }), [backup]);
  assert.deepEqual(inbox.decodeReevaluationList({ items: [reevaluation] }), [reevaluation]);
  assert.deepEqual(inbox.decodeApprovalList({ items: [approval] }), [approval]);
  assert.equal(inbox.decodeConflictList({ items: [{ ...conflict, raw_evidence: "leak" }] }), null);
  assert.equal(inbox.decodeApprovalList({ items: [{ ...approval, owner: "forged" }] }), null);
  assert.equal(inbox.decodeApprovalList({ items: [{ ...approval, request_revision: 0 }] }), null);
});

test("lazy detail decoder는 safe text와 metadata-only evidence grant만 exact 허용한다", () => {
  const detail = { ...conflict, expected_case_revision: 1, expected_request_revision: 2, expected_round: 1, question: "질문", candidates: [{ card_id: "card-1", card_revision: 1, card_digest: sha, owner_user_id: "user-1", concept_ref: "concept-1", coverage_digest: sha }], own_concurrence: null, evidence_grants: [{ grant_id: "grant-1", candidate_card_id: "card-1", candidate_card_revision: 1, concept_ref: "concept-1", expires_at: "2026-08-01T00:00:00Z", single_use: true, status: "available" }] };
  assert.deepEqual(inbox.decodeConflictDetail(detail), detail);
  assert.equal(inbox.decodeConflictDetail({ ...detail, source_uri: "file:///secret" }), null);
  assert.equal(inbox.decodeConflictDetail({ ...detail, evidence_grants: [{ ...detail.evidence_grants[0], content: "raw" }] }), null);
});

async function withFetch(handler, run) { const original = globalThis.fetch; globalThis.fetch = handler; globalThis.document = { cookie: "__Host-aon-central-csrf=csrf-value" }; try { await run(); } finally { globalThis.fetch = original; } }

test("list/detail은 dedicated 13-route 중 해당 exact URI와 AbortSignal만 사용한다", async () => {
  const seen = [];
  await withFetch(async (url, init) => { seen.push([String(url), init]); return new Response(JSON.stringify({ items: [] })); }, async () => {
    const controller = new AbortController();
    await inbox.listConflicts(controller.signal); await inbox.listBackupReviews(controller.signal); await inbox.listReevaluations(controller.signal); await inbox.listApprovals(controller.signal);
  });
  assert.deepEqual(seen.map(([url]) => url), ["/api/inbox/conflicts", "/api/inbox/backup-reviews", "/api/inbox/reevaluations", "/api/inbox/approvals"]);
  assert.ok(seen.every(([, init]) => init.credentials === "same-origin" && init.cache === "no-store"));
});

test("모든 action은 CSRF·replay key·expected revision과 actor-free exact body를 보낸다", async () => {
  const requests = [];
  await withFetch(async (url, init) => { requests.push([String(url), init]); return new Response(JSON.stringify({ receipt_id: "receipt-1", review_id: "review-1", revision: 2, state: "reviewed", correction_record_id: null, replayed: false })); }, async () => {
    await inbox.disposeBackup("review-1", { kind: "approve", rationale: "  확인  ", expected_revision: 1 }, "key-1");
  });
  assert.equal(requests[0][0], "/api/inbox/backup-reviews/review-1/dispositions");
  assert.equal(requests[0][1].headers["X-AON-CSRF"], "csrf-value");
  assert.equal(requests[0][1].headers["Idempotency-Key"], "key-1");
  assert.equal(requests[0][1].body, JSON.stringify({ kind: "approve", rationale: "  확인  ", expected_revision: 1 }));
  for (const claim of ["actor", "actor_id", "org_id", "owner", "owner_id"]) assert.equal(claim in JSON.parse(requests[0][1].body), false);
});

test("required 원문은 whitespace를 보존하되 empty/lone surrogate/64KiB overflow를 client에서 거부한다", async () => {
  await assert.rejects(inbox.disposeReevaluation("reeval-1", { kind: "acknowledge", rationale: "", expected_revision: 1 }, "key-1"), /필수/);
  await assert.rejects(inbox.disposeBackup("review-1", { kind: "correct", corrected_text: "\ud800", rationale: "ok", expected_revision: 1 }, "key-1"), /형식/);
  await assert.rejects(inbox.disposeBackup("review-1", { kind: "correct", corrected_text: "가".repeat(22000), rationale: "ok", expected_revision: 1 }, "key-1"), /65536/);
});

test("401/404/409/503은 session stop·hiding·canonical reload를 위한 typed error다", async () => {
  for (const [status, code] of [[401, "session_unavailable"], [404, "not_found_or_denied"], [409, "stale_or_conflict"], [503, "unavailable"]]) {
    await withFetch(async () => new Response(JSON.stringify({ code, message: "safe" }), { status }), async () => {
      await assert.rejects(inbox.listConflicts(), (error) => error.status === status && error.code === code && error.reload === (status === 409 || status === 503));
    });
  }
});

test("load epoch helper는 마지막 tab/session/detail 응답만 허용한다", () => {
  assert.equal(inbox.isCurrentLoad(4, 4, false), true);
  assert.equal(inbox.isCurrentLoad(3, 4, false), false);
  assert.equal(inbox.isCurrentLoad(4, 4, true), false);
});

test("UI는 네 접근 가능한 탭·Home/End·lazy detail·session unavailable·중복 방지를 갖는다", () => {
  assert.match(ui, /role="tablist"/); assert.match(ui, /role="tabpanel"/); assert.match(ui, /ArrowLeft|ArrowRight/); assert.match(ui, /Home/); assert.match(ui, /End/);
  const tablistStart = ui.indexOf('<div role="tablist"'); const tablist = ui.slice(tablistStart, ui.indexOf("</div>", tablistStart));
  assert.doesNotMatch(tablist, /<Button/);
  assert.match(ui, /\/api\/auth\/session/); assert.match(ui, /AbortController/); assert.match(ui, /loadEpoch/); assert.match(ui, /detailEpoch/); assert.match(ui, /aria-live/); assert.match(ui, /busyAction/);
  assert.match(ui, /reloadSelected/); assert.match(ui, /reason\.status === 404/);
  assert.doesNotMatch(ui, /fetchCaseDocument|\/api\/cases|\/api\/reeval|demo|DEMO_IDENTITIES|raw_evidence/);
});
