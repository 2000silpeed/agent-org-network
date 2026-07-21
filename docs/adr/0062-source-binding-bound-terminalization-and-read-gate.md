# ADR 0062 — S1c.3 v8 Bound companion·SourceReadGate·every-read attestation

- 상태: Proposed
- 날짜: 2026-07-20
- 선행: ADR 0059 S1c.1, S1c.2a, S1c.2b
- 범위: trusted fake integration 위 Bound terminal, per-read authorization/attestation, kill switch·revoke·reconciliation
- 제외: real network/production credential adapter, finding closure, proposal, evaluation, promotion, serving target state write

## 맥락

S1c.2b는 bootstrap-attested trusted fake profile/session과 signed `StableSourceReadback`을 제공하지만, 현재 v7 cycle CHECK와 immutable trigger는 `binding_pending`만 허용한다. v7을 `bound`로 UPDATE하거나 old readback을 Bound evidence로 해석하면 v7 Pending evidence의 불변성과 terminal content/receipt/serving identity 결박이 깨진다. terminalization에는 current Authority, live session lifecycle, v7 Pending generation, **새 Bound용 signed observation**과 continuous enforcement 상태를 같은 결정창에서 대조해야 한다. read allow는 Bound history가 아니라 매 read의 current Authority와 fresh attestation에 근거해야 한다.

## 결정

### 1. v7 immutability와 v8 terminal source of truth

v7은 S1c.1의 immutable `binding_pending` intent/evidence SSOT로 남고 UPDATE하지 않는다. S1c.3은 additive `durable_reciprocal_review_source_binding_v8` companion을 만든다. v8의 terminal projection이 source-binding outcome의 유일한 source of truth다.

```text
V8BindingTerminal = BoundV8 | SupersededV8

logical pending = exact valid v7 Pending intent + v8 terminal row 부재
terminal      = exact valid v7 Pending intent + immutable one v8 terminal row
```

`BoundV8` 또는 `SupersededV8` insertion은 source worker lease winner만 요청할 수 있는 one-way terminal이며 `(org_id, v7_intent_id)` unique key의 single winner다. v8 row 부재는 임의 default Bound가 아니라 v7 Pending에 대한 **non-terminal** projection일 뿐이며, payload release에는 항상 validated `BoundV8`가 필요하다.

terminal UoW는 DB-time transaction에서 다음을 모두 exact verify한다.

- S1c.1 pending intent/semantic receipt/upstream v2/v5 reconstruction, org, `SourceResourceRef`, expected source revision, artifact revision/content digest, binding generation
- non-expired current worker lease and S1c.2b live trusted fake session identity/profile version/digest/opaque credential-ref generation
- registry-key-verified signed `BoundSourceReadbackV8`, same v7 intent semantic digest and Authority receipt payload digest, structured source/session identity, expected/observed source revision, artifact revision/content digest, binding generation and enforcement identity; no stale or post-close observation
- `SourceBoundaryEnforcer` Pending→Active result. fake enforcement reference도 no-bypass, self-deny while Pending, Active identity/generation/receipt binding을 simulate해야 한다.
- same sealed bootstrap capability의 `verify_current(..., purpose=BoundTerminal)` result, including current policy/boundary/declassification/revocation/kill-switch state
- no existing terminal and expected Pending generation CAS

`BoundSourceReadbackV8`은 S1c.2b `StableSourceReadback`을 mutate·upgrade·추정 복구하지 않는 별 signed canonical observation이다. live trusted fake session이 v7 intent와 current Authority context를 재대조한 뒤에만 발급하며, `artifact_revision_id`, `artifact_content_digest`, `authority_receipt_id`, `authority_receipt_payload_digest`, source/expected/observed revision, profile/session identity, binding generation, external generation, enforcement identity/state, issued/expiry를 모두 서명한다. old stable readback은 v8 발급의 입력 관측일 수 있을 뿐 terminal/read gate evidence가 아니다.

그 뒤에만 immutable `SourceBoundaryEnforcementReceiptV8`, `BindingReceiptV8`, `BoundV8` terminal/audit/outbox와 Active reference를 하나의 transaction에 insert한다. external source apply/readback/enforcement I/O는 DB lock 밖에서 완료하고 terminal transaction은 observation을 재검증할 뿐 I/O하지 않는다. application success, old stable readback alone, public DTO, expired/closed session, static/scheduler/webhook protection, Authority unavailable, missing Active enforcement, any mismatch는 Bound 0이다.

### 2. additive v8 schema and semantic validator

S1c.3 is additive to v7 Pending/readback evidence; no v7 DDL, trigger, row or manifest is changed.

- `source_binding_v8_bound_readbacks`: immutable `BoundSourceReadbackV8` canonical payload/digest/key ID/signature, `(org_id, v7_intent_id, observation_id)` and full artifact-content/Authority-receipt/session/enforcement binding.
- `source_binding_v8_enforcement_references`: `(org_id, v7_intent_id, binding_generation)` immutable lifecycle `Pending | Active | Releasing | Archived`, profile/session identity digest and `SourceBoundaryEnforcementReceiptV8` digest; one active protection scope only.
- `source_binding_v8_terminals`: intent one-to-one immutable `BoundV8 | SupersededV8` canonical terminal payload/digest, exact v7 intent digest, v8 bound-readback/enforcement/Authority digest, terminal audit/outbox IDs and composite FKs.
- `source_binding_v8_read_attestation_evidence`: append-only `SourceServingAttestationV8` verification evidence with source/profile/session identity, observed revision/content, binding generation, v8 terminal/active-enforcement/Authority receipt digest, issued/expiry and canonical digest.
- `source_binding_v8_kill_switches`: immutable authorized kill/revoke receipt/event, exact v8 protection scope and effective DB time; a current active kill is a deny condition, not a rewrite of `BoundV8` history.

All tables use strict manifest, org-scoped composite keys/FKs to v7 intent, exact-key JSON, immutable triggers and validator reconstruction. Validator first runs the v7 component validator and re-queries its exact Pending semantic graph; then reconstructs v8 payloads and verifies current registry keys/signatures. It rejects terminal/reference/attestation/kill graph gaps, foreign source/profile/session, duplicate active/terminal, noncanonical digest, any terminal based solely on `StableSourceReadback`, or `BoundV8` without its exact Active enforcement and `BoundSourceReadbackV8`. v8 is not a projection cache: a missing, malformed or spoofed v8 row denies reads and never falls back to v7.

### 3. SourceReadGate

`SourceReadGate.authorize_read(request)` runs before every payload access; it does not cache an allow decision. It obtains a fresh signed `SourceServingAttestationV8` from the live trusted fake session and performs, in order:

1. current kill switch/revocation/profile/session lifecycle check;
2. sealed Authority `verify_current(..., purpose=SourceRead)`;
3. validator-backed exact lookup of `BoundV8` terminal and Active v8 enforcement reference;
4. signature/canonical/key/expiry verification of fresh `SourceServingAttestationV8` against source ref, observed revision/content digest, artifact revision/content digest, binding generation, profile/session identity, v7 intent semantic digest, Authority receipt payload digest and v8 terminal/enforcement digest;
5. payload release only on the opaque Verified Authority arm.

Pending/Releasing/Archived or absent Active, v8 terminal absence, closed/rotated session, unavailable Authority, expired/revoked receipt, stale/unsigned/mismatched v8 attestation, static policy copy, or an active kill switch is deny before payload. A v7 Pending row, old stable readback, or a forged v8 projection cannot open the gate. A kill/revoke acts immediately through gate denial and Pending/Active enforcement's deny mode; any convergence failure is unavailable, never fail-open. This fake-only slice does not claim real gateway/native or network enforcement.

### 4. failures, reconciliation and supersession

terminalization crashes before v8 commit leave v7 Pending plus attempt/readback/enforcement evidence; recovery reissues a fresh v8 Bound observation and revalidates current session/Authority rather than replaying old stable/v8 evidence. Crash after `BoundV8` commit only replays immutable v8 result. Authority/profile/session/boundary drift before terminal is Pending+escalation and future apply/read deny; it does not silently erase `BoundV8`.

`SupersededV8` requires S1c.2a lease fence plus current fake session's signed non-mutation or non-serving cleanup observation and no Active v8 reference. A late observation or mutation after Superseded appends `LateBindingMutationObserved`, activates deny/kill as applicable, and never inserts/resurrects BoundV8. Human-only `SourceReconciliationReceipt` must bind the exact failed v7 intent, current Authority/boundary, kill/revoke state, cleanup observation, issuer/policy and fence before a new cycle is considered.

### 5. minimal ports

```text
SourceBindingTerminalizer
  finalize(v7_intent_id, lease, BoundSourceReadbackV8, pending_enforcement, db_now)
    -> BoundTerminal | PendingUnchanged | Unavailable

TrustedFakeBoundaryEnforcer
  activate(pending_reference, BoundSourceReadbackV8, verified_bound_authority)
    -> ActiveEnforcement | Denied | Unavailable
  attest_read(bound_reference, db_now)
    -> SourceServingAttestationV8 | Denied | Unavailable
  deny(scope, kill_or_revoke_receipt) -> DenyApplied | Unavailable

SourceReadGate
  authorize_read(source_ref, request_context, db_now)
    -> VerifiedSourceBindingAuthorization | Denied | Unavailable

SourceBindingKillSwitchAuthority
  arm(scope, reason, db_now) -> KillSwitchReceipt | Denied | Unavailable
  current(scope, db_now) -> ActiveKill | NoKill | Unavailable
```

Only the sealed bootstrap composition creates these ports from the trusted fake profile/session. Callers and adapters may provide observations but cannot manufacture Authority resolution, `BoundSourceReadbackV8`, Active enforcement, v8 attestation, kill receipt or terminal result.

## implementation gate tests

- v7 CHECK/trigger/catalog/row remain byte-for-byte valid `binding_pending`; v8 migration never UPDATEs v7. exact valid v7 Pending alone may be terminalization input, but it is never a terminal/read allow result without the complete validated v8 graph.
- exact one BoundV8 winner under 32-way terminal attempts; wrong/expired lease, stale v7 Pending generation, duplicate terminal and session close/rotation are Bound 0.
- every precondition field mismatch (org/source/profile/session/credential-ref generation/v8 readback signature or key/revision/artifact content/binding generation/Authority receipt/enforcement/Authority policy-boundary-declassification) is Bound 0. Old `StableSourceReadback` alone or coerced/copy-constructed v8 DTO is rejected.
- Pending→Active and BoundV8/audit/outbox terminal are atomic; injected faults leave v7 Pending or one complete v8 Bound graph, never partial Active/Bound.
- every read invokes current `SourceRead`, fresh v8 attestation and kill check. cached Bound, v7-only row, old stable/v8 attestation, missing/Inactive reference, Authority unavailable/revoked/expired, kill/revoke and session lifecycle drift deny before fake payload.
- crash-before/after terminal, outbox replay, uncertain readback, late commit/mutation, kill during terminal/read and reconciliation require the stated Pending/escalation/deny/late-observation paths with no BoundV8 resurrection.
- no real network, mTLS transport, production credential material, proposal/eval/promotion/serving target state is opened or written.

## 결과

S1c.3 gives the trusted fake path a v8 terminal/read authorization model without mutating or over-claiming v7 Pending evidence. Real network and production credential adapters remain a later, separately authorized slice.
