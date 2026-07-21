# ADR 0060 — v7 Source Binding worker apply·exact read-back·Bound

- 상태: Proposed
- 날짜: 2026-07-20
- 선행: ADR 0059 S1c.1 v7 `BindingPending` intent
- 범위: Pending outbox delivery, fenced external source apply, stable exact read-back, continuous source enforcement, `Bound | Superseded` terminal과 every-read authorization
- 제외: finding closure, proposal, evaluation, promotion, serving target state write

## 맥락

S1c.1은 bootstrap-attested central Authority가 발급한 signed v7 Pending intent와 immutable evidence를 기록할 뿐 source를 호출하거나 read를 허용하지 않는다. source adapter의 성공 응답이나 공개 DTO는 Authority 증명이 아니며, 외부 CAS 호출은 DB transaction 안에서 수행하면 안 된다. 반대로 timeout·crash·late commit이 있을 때 근거 없이 Pending을 Supersede하면 이미 source가 변경되었을 가능성을 잃는다.

## 결정

### 1. additive worker/terminal ledger

S1c.2는 S1c.1 intent를 수정·재발급하지 않고 다음 v7 companion만 additive로 소유한다.

- `source_binding_v7_worker_leases`: `(org_id, intent_id)`의 active lease 하나, monotonic `lease_epoch`, worker identity, DB-time expiry와 token hash. full token은 claim 반환 때만 존재한다.
- `source_binding_v7_apply_attempts`: `(org_id, intent_id, attempt_no)` append-only attempt, lease epoch, canonical external operation digest/idempotency key, dispatch/observation classification과 timestamps.
- `source_binding_v7_readbacks`: append-only exact observation. structured `SourceResourceRef`, expected/observed source revision, external version/generation, artifact revision/content digest, binding generation, enforcement identity/reference/digest 및 observation digest를 저장한다.
- `source_binding_v7_terminals`: intent당 하나의 immutable `Bound` 또는 `Superseded` terminal receipt/result/audit/outbox graph. cycle transition은 expected pending generation CAS로만 가능하다.

모든 key는 `org_id`를 포함하고 canonical manifest, strict exact-key JSON, immutable triggers, composite FK와 intent당 active lease/terminal 하나의 DB constraint를 가진다. S1c.1의 persistent SQLite target/source identity fence와 receipt semantic preimage를 그대로 재검증한다. terminal receipt는 source apply 응답이 아니라 readback/enforcement/Authority verification의 결과다.

### 2. worker와 외부 source 호출

outbox consumer는 짧은 DB transaction에서 Pending intent의 current semantic graph를 validate하고 DB-time lease를 claim한다. DB lock을 release한 뒤에만 worker가 다음 순서로 실행한다.

1. sealed bootstrap Authority capability로 `verify_current(..., purpose=SourceApply)`를 수행한다.
2. trusted integration registry가 `SourceResourceRef`의 production-enabled profile을 현재 resolve한다. profile은 expected-revision CAS, semantic idempotency, worker fence, stable exact read-back, gateway/native continuous enforcement 및 every-read attestation을 모두 보장해야 한다.
3. `SourceBindingExecutor.apply()`에는 structured source ref, exact expected source revision, artifact revision/content digest, boundary/enforcement plan digest, intent semantic digest/idempotency key, binding generation, lease epoch/fence만 전달한다. adapter는 권한·classification·boundary를 발급하지 않고 conditional external CAS/idempotent operation과 관측만 한다.
4. worker는 fresh stable exact read-back과 enforcement observation을 얻는다. retry는 같은 intent semantic digest와 external idempotency key만 사용하며 새 effect를 만들지 않는다.

apply 직전과 terminal 직전에 worker lease epoch/expiry, intent Pending generation, source identity, Authority result를 다시 확인한다. stale worker/lease는 외부 call과 terminal write 모두 0이다.

### 3. Bound와 continuous enforcement

gateway arm은 no-bypass `SourceGatewaySubject` Pending reference를 source visibility 전 만들고 terminal에서 verified Pending→Active를 원자화한다. native arm은 revision/content, binding generation, boundary mode와 `NativeServingBinding(Pending)`을 source external-version CAS에 같이 적용하며 Pending 동안 self-deny한다. 둘 중 어느 것도 atomic Pending/self-deny/generation fence를 제공하지 않으면 해당 source integration은 production에서 disable한다.

terminal UoW는 짧은 DB transaction에서 only the current lease winner의 fresh observation을 다시 validate하고 `verify_current(..., purpose=BoundTerminal)`를 호출한다. Authority가 reconstructed current context와 structured source ref, expected revision, artifact/content digest, binding generation, Active enforcement receipt 및 readback을 exact match한 경우에만 `BindingPending → Bound` CAS와 terminal receipt/audit/outbox를 원자화한다. stale/partial/mismatched observation, expired/revoked authorization, source revision drift, pending/releasing/unknown protection, static copied ACL 또는 scheduler/webhook/poller-only protection은 Bound write 0이다.

Bound 뒤에도 `SourceReadGate`는 payload를 반환하기 전에 `verify_current(..., purpose=SourceRead)`와 fresh `SourceServingAttestation`을 요구한다. attestation은 source identity, revision/content digest, binding generation, active receipt/enforcement identity를 exact bind한다. missing/unknown/mismatch/expired/revoked/Pending/Releasing/Archived는 deny다. Bound 이력은 current read allow의 대체물이 아니다.

### 4. uncertainty, recovery, reconciliation

timeout, transport failure, worker crash, source rejection 뒤 late mutation 가능성, readback failure 또는 Authority unavailable은 **uncertain**이다. worker는 attempt/observation과 escalation outbox를 남기고 Pending을 유지한다. recovery scanner는 expired lease를 fenced reclaim하고 같은 external idempotency key로 retry한다. outbox는 at-least-once delivery이며 duplicate delivery는 one active lease와 source semantic idempotency로 수렴한다.

`Superseded`는 source expected-revision CAS, semantic idempotency, worker fencing 및 stable exact read-back이 old attempt의 additional mutation 0 또는 exact non-serving cleanup을 증명한 경우에만 쓸 수 있다. 일정/attempt 상한은 자동 Bound/Supersede 근거가 아니며 human escalation을 연다. supersede 뒤 unexpected source mutation은 `LateBindingMutationObserved`로 append하고 old cycle을 Bound로 되살리지 않는다. source owner의 human-only `SourceReconciliationReceipt`가 current Authority/boundary, cleanup/readback, issuer/policy, exact failed intent와 fencing을 결박한 경우에만 별 후속 cycle을 검토할 수 있다.

governance/boundary/declassification drift는 Pending을 즉시 Bound/Superseded로 바꾸지 않고 future apply를 막고 escalation을 남긴다. pre-authorized source deny/unpublish action 외에 worker가 source를 보정·publish·unpublish하지 않는다.

### 5. minimal port shape

```text
SourceBindingWorkerLeaseStore
  claim(intent_id, worker_identity, db_now) -> ClaimedLease | LeaseUnavailable
  revalidate(lease, db_now) -> CurrentLease | LeaseLost

SourceBindingExecutor
  apply(VerifiedSourceBindingAuthorization, SourceApplyRequest, lease_fence)
    -> SourceApplyObservation | SourceApplyUncertain
  read_back(SourceReadbackRequest, lease_fence)
    -> SourceReadbackObservation | SourceReadbackUncertain

SourceBoundaryEnforcer
  verify_active(readback, VerifiedSourceBindingAuthorization)
    -> SourceBoundaryEnforcementObservation | EnforcementUnavailable

SourceReadGate
  authorize_read(source_ref, serving_attestation, db_now)
    -> VerifiedSourceBindingAuthorization | Denied | Unavailable
```

`VerifiedSourceBindingAuthorization`은 ADR 0059 Authority가 same sealed bootstrap capability에서 발급한 opaque resolution이다. `SourceApplyObservation`, `SourceReadbackObservation`, `SourceBoundaryEnforcementObservation`, `SourceServingAttestation`은 adapter/enforcer 관측 DTO이며 어떤 권한도 self-assert하지 않는다. UoW는 관측과 opaque Authority resolution의 exact binding을 함께 검사한다.

## 구현 게이트 테스트

- lease claim/reclaim/renew crash와 32-way worker 경쟁에서 active winner 하나, stale epoch external call/terminal write 0, full token persistence 0을 검증한다.
- DB lock이 external apply/readback/enforcement 동안 잡히지 않고, 동일 intent의 duplicate outbox/recovery가 같은 idempotency key·one source effect로 수렴함을 검증한다.
- wrong org/source kind/source ID/expected revision/artifact/content/binding generation/semantic digest/lease fence, Authority apply/terminal expiry·revocation·policy/boundary/declassification drift는 write 0이다.
- gateway Pending→Active 및 native Pending self-deny/external-version CAS, static ACL·scheduler-only·missing/old/partial/mismatched readback 또는 enforcement observation의 Bound 0을 검증한다.
- every read는 current Authority `SourceRead` resolution과 fresh matching attestation 없이는 deny하며, Bound receipt만으로 read가 열리지 않음을 검증한다.
- timeout, crash-after-apply, crash-before-terminal, readback unavailable, source revision conflict, late commit, lease loss, outbox redelivery는 Pending/uncertain+escalation으로 남는다. non-mutation/cleanup을 fenced readback으로 증명한 경우만 Supersede하며 late mutation은 Bound resurrection 없이 human reconciliation으로 간다.
- v6 row/receipt는 worker input, Bound, read allow로 승격되지 않고 S1c.2가 proposal/eval/promotion/serving target state를 write하지 않음을 검증한다.

## 결과

S1c.2는 S1c.1의 Authority-issued Pending을 실제 source operation으로 안전하게 이어 주지만, source integration이 모든 required capability를 증명할 때만 연다. 이 ADR이 구현되기 전에는 external apply, Bound, source mutation/read authorization은 계속 0이다.
