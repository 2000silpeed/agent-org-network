import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { Buffer } from "node:buffer";
import ts from "typescript";

const source = await readFile(new URL("./onboarding-api.ts", import.meta.url), "utf8");
const ownerProxySource = await readFile(
  new URL("../app/owner-api/[...path]/route.ts", import.meta.url),
  "utf8",
);
const transpiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  fileName: "onboarding-api.ts",
  reportDiagnostics: true,
});
assert.deepEqual(transpiled.diagnostics ?? [], []);
const { createOwnerAuthoringDraft, OnboardingError, getOnboardingStatus, registerAgentCard, registerRegistryUser } = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.outputText).toString("base64")}`
);

test("OKF 문서는 Card Owner endpoint로만 가고 durable review receipt 뒤 완료된다", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: "aon_owner_csrf=csrf-proof" };
  await withFetch(async (input, init) => {
    assert.equal(input, "/owner-api/authoring/runs");
    assert.equal(new Headers(init.headers).get("idempotency-key"), "author-1");
    assert.equal(new Headers(init.headers).get("x-owner-csrf"), "csrf-proof");
    const body = JSON.parse(init.body);
    assert.equal(body.agent_id, "support");
    assert.equal(body.documents[0].source_id, "policy.md");
    assert.equal(body.documents[0].content_base64, "UFJJVkFURQ==");
    return new Response(JSON.stringify({
      run_id: "run-1", stage: "AwaitingOwnerReview", revision: 1,
      document_count: 1, edge_count: 0, dropped_count: 0,
    }));
  }, async () => {
    const result = await createOwnerAuthoringDraft(
      "support",
      [new File(["PRIVATE"], "policy.md", { type: "text/markdown" })],
      "author-1",
    );
    assert.equal(result.stage, "AwaitingOwnerReview");
  });
  globalThis.document = originalDocument;
});

test("Owner proxy는 exact route와 명시 env 및 header allowlist만 사용한다", () => {
  assert.match(ownerProxySource, /AON_OWNER_BACKEND_URL/);
  assert.doesNotMatch(ownerProxySource, /127\.0\.0\.1|localhost|\?\?/);
  assert.match(ownerProxySource, /context\.params\.path\.length !== 2/);
  assert.match(ownerProxySource, /request\.nextUrl\.search !== ""/);
  for (const forbidden of ["authorization", "forwarded", "x-forwarded", "headers = new Headers(request.headers)"]) {
    assert.doesNotMatch(ownerProxySource, new RegExp(forbidden));
  }
  assert.match(ownerProxySource, /aon_owner_session/);
  assert.match(ownerProxySource, /x-owner-csrf/);
  assert.doesNotMatch(ownerProxySource, /new Headers\(response\.headers\)/);
  for (const forbiddenResponseHeader of ["set-cookie", "location", "access-control-allow"]) {
    assert.doesNotMatch(ownerProxySource, new RegExp(forbiddenResponseHeader));
  }
});

async function withFetch(fn, run) {
  const original = globalThis.fetch;
  globalThis.fetch = fn;
  try {
    await run();
  } finally {
    globalThis.fetch = original;
  }
}

test("온보딩 status는 User·Card·Knowledge 순서와 SSO 파생 상태를 만든다", async () => {
  let calls = 0;
  await withFetch(async (input) => {
    calls += 1;
    if (input === "/api/onboarding/status") {
      return new Response(JSON.stringify({
        revision: 2, card_capability: "available", cards: [],
        steps: [
          { kind: "user", state: "complete" },
          { kind: "card", state: "current" },
          { kind: "knowledge", state: "locked" },
        ],
      }));
    }
    assert.equal(input, "/api/admin/users");
    return new Response(
      JSON.stringify([
        { user_id: "root", email: "root@company.com", manager: null },
        {
          user_id: "alice",
          email: "alice@company.com",
          manager: "root",
          sso_link_status: "verified_email_match",
        },
      ]),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  }, async () => {
    const status = await getOnboardingStatus();
    assert.equal(calls, 2);
    assert.equal(status.revision, 2);
    assert.equal(status.steps[0].kind, "user");
    assert.equal(status.steps[0].state, "complete");
    assert.equal(status.steps[1].kind, "card");
    assert.equal(status.steps[1].state, "current");
    assert.equal(status.steps[2].kind, "knowledge");
    assert.equal(status.steps[2].state, "locked");
    assert.equal(status.users[1].sso_link_status, "verified_email_match");
  });
});

test("Agent Card command는 live endpoint와 exact response를 사용한다", async () => {
  await withFetch(async (input, init) => {
    assert.equal(input, "/api/admin/agent-cards");
    assert.equal(new Headers(init.headers).get("idempotency-key"), "card-1");
    const command = JSON.parse(init.body);
    assert.equal(command.expected_revision, 1);
    assert.equal(command.org_id, undefined);
    return new Response(JSON.stringify({
      card: {
        agent_id: "support", owner: "root", team: "support", summary: "Support",
        domains: ["support"], last_reviewed_at: "2026-07-27", maintainer: null,
        can_answer: [], cannot_answer: [], approval_when: [], collaborate_when: [],
        knowledge_sources: [], trust_labels: [],
      },
      revision: 2,
      replayed: false,
    }), { status: 200, headers: { "content-type": "application/json" } });
  }, async () => {
    const result = await registerAgentCard({
      expected_revision: 1, agent_id: "support", owner: "root", team: "support",
      summary: "Support", domains: ["support"], maintainer: null, can_answer: [],
      cannot_answer: [], approval_when: [], collaborate_when: [],
      knowledge_sources: [], trust_labels: [],
    }, "card-1");
    assert.equal(result.card.agent_id, "support");
  });
});

test("온보딩 status의 preview-shaped·missing·extra Card는 503으로 닫는다", async () => {
  const invalidCards = [
    { agent_id: "support", summary: "preview" },
    {
      agent_id: "support", owner: "root", team: "support", summary: "Support",
      domains: ["support"], last_reviewed_at: "2026-07-27", maintainer: null,
      can_answer: [], cannot_answer: [], approval_when: [], collaborate_when: [],
      knowledge_sources: [],
    },
    {
      agent_id: "support", owner: "root", team: "support", summary: "Support",
      domains: ["support"], last_reviewed_at: "2026-07-27", maintainer: null,
      can_answer: [], cannot_answer: [], approval_when: [], collaborate_when: [],
      knowledge_sources: [], trust_labels: [], token: "secret",
    },
  ];
  for (const card of invalidCards) {
    await withFetch(async (input) => new Response(JSON.stringify(
      input === "/api/onboarding/status"
        ? {
          revision: 2, card_capability: "available", cards: [card],
          steps: [
            { kind: "user", state: "complete" },
            { kind: "card", state: "complete" },
            { kind: "knowledge", state: "current" },
          ],
        }
        : [{ user_id: "root", email: "root@company.com", manager: null, sso_link_status: "verified_email_match" }],
    )), async () => {
      await assert.rejects(
        getOnboardingStatus(),
        (error) => error instanceof OnboardingError && error.status === 503,
      );
    });
  }
});

test("미등록 현재 신원은 SSO link를 추정하지 않는다", async () => {
  await withFetch(
    async (input) =>
      new Response(JSON.stringify(input === "/api/onboarding/status"
        ? {
          revision: 1, card_capability: "available", cards: [],
          steps: [
            { kind: "user", state: "complete" },
            { kind: "card", state: "current" },
            { kind: "knowledge", state: "locked" },
          ],
        }
        : [{ user_id: "root", email: "root@company.com", manager: null }])),
    async () => {
      const status = await getOnboardingStatus();
      assert.equal(status.steps[0].state, "current");
      assert.equal(status.users[0].sso_link_status, "not_current_principal");
    },
  );
});

test("등록 command는 idempotency key를 header로 보내고 actor와 org를 body에 넣지 않는다", async () => {
  await withFetch(async (input, init) => {
    assert.equal(input, "/api/admin/users");
    assert.equal(init.method, "POST");
    assert.equal(new Headers(init.headers).get("idempotency-key"), "command-1");
    assert.deepEqual(JSON.parse(init.body), {
      expected_revision: 1,
      user_id: "alice",
      email: "alice@company.com",
      manager: "root",
    });
    return new Response(
      JSON.stringify({
        user_id: "alice",
        email: "alice@company.com",
        manager: "root",
        revision: 2,
        replayed: false,
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  }, async () => {
    const result = await registerRegistryUser(
      { expected_revision: 1, user_id: "alice", email: "alice@company.com", manager: "root" },
      "command-1",
    );
    assert.equal(result.user_id, "alice");
  });
});

for (const code of [403, 409, 422, 503]) {
  test(`등록 HTTP ${code}는 typed OnboardingError로 보존된다`, async () => {
    await withFetch(
      async () =>
        new Response(JSON.stringify({ detail: { errors: ["실패"] } }), {
          status: code,
          headers: { "content-type": "application/json" },
        }),
      async () => {
        await assert.rejects(
          registerRegistryUser(
            { expected_revision: 1, user_id: "alice", email: "alice@company.com", manager: null },
            "command-1",
          ),
          (error) => error instanceof OnboardingError && error.status === code,
        );
      },
    );
  });
}

test("성공 응답은 exact shape만 허용하고 추가·누락·위험 타입은 503으로 닫는다", async () => {
  const invalid = [
    { user_id: "alice", email: "alice@company.com", manager: null, revision: 2 },
    {
      user_id: "alice",
      email: "alice@company.com",
      manager: null,
      revision: 2,
      replayed: false,
      token: "secret",
    },
    {
      user_id: "alice@example.com",
      email: "alice@company.com",
      manager: null,
      revision: 2,
      replayed: false,
    },
  ];
  for (const body of invalid) {
    await withFetch(
      async () =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      async () => {
        await assert.rejects(
          registerRegistryUser(
            {
              expected_revision: 1,
              user_id: "alice",
              email: "alice@company.com",
              manager: null,
            },
            "command-1",
          ),
          (error) => error instanceof OnboardingError && error.status === 503,
        );
      },
    );
  }
});

test("서버 detail과 본문은 오류 메시지에 반사하지 않는다", async () => {
  const secret = "TOKEN-DO-NOT-REFLECT";
  for (const code of [401, 403, 409, 422, 503, 500]) {
    await withFetch(
      async () =>
        new Response(JSON.stringify({ detail: secret, token: secret }), {
          status: code,
          headers: { "content-type": "application/json" },
        }),
      async () => {
        await assert.rejects(
          registerRegistryUser(
            {
              expected_revision: 1,
              user_id: "alice",
              email: "alice@company.com",
              manager: null,
            },
            "command-1",
          ),
          (error) =>
            error instanceof OnboardingError &&
            !error.message.includes(secret) &&
            !error.message.includes(String(code)),
        );
      },
    );
  }
});
