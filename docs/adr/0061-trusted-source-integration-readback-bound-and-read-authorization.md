# ADR 0061 — S1c.2b trusted source integration·Bound·every-read authorization

- 상태: Proposed
- 날짜: 2026-07-20
- 선행: ADR 0059 S1c.1 Pending intent, S1c.2a fake-only worker observation, ADR 0060
- 범위: production-enabled source integration profile, fenced real adapter apply/readback, continuous enforcement, Bound, source read authorization/revoke
- 제외: finding closure, proposal, evaluation, promotion, serving target state write

## 맥락

S1c.2a의 `FakeSourceBindingExecutor`는 retry/lease/observation shape만 검증하며 실제 source mutation, trusted integration profile, stable readback, Bound 또는 source read를 열지 않는다. real adapter가 endpoint/credential/capability를 자기보고하거나 static ACL·scheduler만 제공하면 Authority와 data boundary를 우회할 수 있다. source apply 성공은 visibility·read authorization·stable state를 증명하지 않는다.

## 결정

### 1. bootstrap-attested trusted integration profile

`bootstrap_authorized_production()`은 immutable central integration registry를 sealed production wiring에 포함한다. registry만 `SourceIntegrationProfile`을 validate·open·revoke하며 caller DI, adapter constructor, endpoint/credential callback은 profile을 만들거나 바꾸지 못한다.

profile은 canonical ID/version/digest, org and structured `SourceResourceRef` scope, external account/target fingerprint, endpoint identity and TLS server policy, mTLS client identity reference, opaque credential reference/rotation generation, source observation signing-key set, allowed boundary enforcement modes, expected-revision CAS/semantic-idempotency/worker-fence/readback/every-read capability, kill-switch/revocation generation을 결박한다. raw private key, bearer token, password, credential material, source body는 SQLite intent/attempt/readback/audit/outbox/exception에 저장·반환·로그하지 않는다. bootstrap capability가 secret manager/HSM에서 mTLS/credential을 use-only로 resolve하고 adapter session에 전달한다. unavailable/rotation/profile drift는 fail-closed이며 persisted opaque reference는 secret이 아니다.

`TrustedSourceIntegrationRegistry.open(profile_id, sealed_bootstrap)`만 mutually-authenticated `TrustedSourceBindingExecutor`와 `SourceBoundaryEnforcer`를 만든다. session은 profile version/digest, mTLS peer identity and credential generation에 bound되고 lifecycle close/revoke에 즉시 닫힌다. Python object privacy는 security proof가 아니며, trusted-process 범위를 넘는 secret isolation은 secret manager/HSM/process isolation의 책임이다.

### 2. real apply and stable signed readback

worker는 DB lock 밖에서 current profile을 resolve하고 Authority `verify_current(..., SourceApply)`와 current lease fence를 직전 재검증한 뒤에만 executor를 호출한다. `SourceApplyRequest`는 profile ID/version/digest, structured source ref, external target fingerprint, expected source revision, artifact revision/content digest, boundary/enforcement plan digest, receipt ID/payload digest, intent semantic digest/external idempotency key, binding generation, worker lease epoch을 exact 포함한다.

adapter는 mTLS-bound conditional CAS와 semantic idempotency를 source에 전달한다. source가 lease fence를 직접 지원하지 않으면 profile-owned fencing proxy가 source operation key에 epoch를 bind하고 stale epoch effect를 0으로 증명해야 한다. CAS/idempotency/fence 중 하나라도 없으면 production profile은 enable하지 않는다.

`StableSourceReadback`은 trusted source observation signer가 canonical payload에 profile fingerprint, source ref, expected/observed revision, external version/generation, artifact revision/content digest, binding generation, receipt/payload digest, enforcement identity/mode/state, observed_at를 서명한 DTO다. registry의 current signing-key set으로 signature·canonical digest를 검증하고, same external generation의 repeat read 또는 source-declared stability proof가 있을 때만 stable이다. apply response, unsigned DTO, readback 한 번의 best effort, stale signing key, profile/identity mismatch는 stable proof가 아니다.

### 3. continuous enforcement, Bound and source reads

gateway profile은 source visibility 전에 `SourceGatewaySubject` Pending route/reference를 no-bypass topology와 함께 arm하고, stable readback과 Authority `BoundTerminal` verification 뒤에만 Pending→Active로 전이한다. native profile은 source external-version CAS에 `NativeServingBinding(Pending)`·revision/content·binding generation·boundary mode를 함께 쓰고 Pending에서 self-deny해야 한다. Active enforcement receipt는 same source identity/generation/receipt/readback을 bind한다. static copied ACL, scheduler, webhook, poller, post-hoc DLP는 continuous enforcement가 아니다.

terminal UoW는 current lease, Pending intent generation, current profile, signed stable readback, Active enforcement receipt와 `verify_current(..., BoundTerminal)`를 exact 대조한다. 모두 통과한 단일 CAS에서만 Bound/terminal receipt/audit/outbox 및 gateway/native Active transition을 기록한다. uncertainty, profile/credential rotation, source revision/identity/content mismatch, missing self-deny/no-bypass, missing/invalid/stale signed readback, inactive enforcement, Authority unavailable/revoked/expired/boundary drift는 Bound 0이다.

`SourceReadGate`는 payload 전마다 current profile/kill-switch 상태, Authority `verify_current(..., SourceRead)`, fresh signed `SourceServingAttestation`을 함께 요구한다. attestation은 source ref, observed revision/content digest, binding generation, active receipt/payload digest, profile version/digest, enforcement identity/state를 bind한다. missing/unknown/mismatch/Pending/Releasing/Archived/revoked/expired 어느 조건도 즉시 deny다. Bound 이력은 per-read authorization을 대신하지 않는다.

### 4. revoke, kill switch, recovery and reconciliation

central Authority revocation, integration profile disable/rotation, boundary/declassification expiry or human kill switch immediately closes new apply/Bound/read. gateway/native must converge to deny; convergence failure is availability failure but read remains deny. `SourceDenyReads | SourceUnpublish` is a separate pre-authorized, expected-revision/idempotent/fenced source action and never an implicit retry cleanup.

timeout, crash, network ambiguity, credential/session failure, lost lease, unsigned or unstable readback, and possible late commit remain Pending/uncertain with durable attempt/observation/escalation evidence. recovery uses the same external idempotency key and new fenced lease only. `Superseded` needs exact non-mutation or non-serving cleanup proof by current profile, source CAS/idempotency/fence and stable signed readback; otherwise human escalation persists. post-supersede mutation is `LateBindingMutationObserved`, never Bound resurrection; a human-only `SourceReconciliationReceipt` binds current Authority/boundary, source cleanup/readback, failed intent, profile, issuer/policy and fence before any follow-up cycle.

### 5. additive types, schema and ports

```text
SourceIntegrationProfile (central immutable configuration; never command input)
TrustedSourceBindingExecutor (mTLS/opaque-credential session)
StableSourceReadback (signed observation, no source body)
SourceBoundaryEnforcementReceipt = Gateway | Native
SourceServingAttestation (signed per-read observation)
SourceIntegrationRevocation / SourceKillSwitchReceipt

TrustedSourceIntegrationRegistry
  open(profile_id, sealed_bootstrap) -> TrustedExecutor | Unavailable
  verify_readback(profile, readback) -> StableReadback | Denied | Unavailable

TrustedSourceBindingExecutor
  apply(request, lease_fence) -> ApplyObservation | Uncertain
  read_back(request, lease_fence) -> SignedReadback | Uncertain

SourceBoundaryEnforcer
  arm_pending(...) -> PendingEnforcement | Unavailable
  activate(...) -> ActiveEnforcement | Denied | Unavailable
  attest_read(...) -> SourceServingAttestation | Denied | Unavailable
  deny_or_unpublish(...) -> only pre-authorized drift action
```

Additive SQLite companions persist no secret: profile ID/version/digest and external target fingerprint bound to intent; signed readback canonical payload/digest/key ID; enforcement reference lifecycle `Pending | Active | Releasing | Archived`; immutable terminal/revocation/kill-switch/audit/outbox evidence; and lease/attempt/readback composite FKs. Runtime open reconstructs receipt/profile/readback/terminal semantics, verifies current registry keys and rejects profile swap, foreign source, unrecognized state, partial graph or noncanonical row. The central profile registry itself is bootstrap configuration, not a caller-writable SQLite source-binding table.

## implementation gate tests

- bootstrap composition rejects arbitrary adapter/profile/endpoint/current callback; profile scope/version/digest, mTLS peer policy, opaque credential generation/rotation/revoke and lifecycle close are exact. No secret appears in row, DTO, audit, outbox, error or test assertion.
- wrong org/source/external target/profile version/digest/mTLS peer/credential generation, CAS/idempotency/fence absence, stale lease or stale profile yields external apply and Bound 0.
- duplicate outbox/recovery/crash-after-apply/crash-before-terminal uses one external idempotency key; late/ambiguous effect stays Pending+escalation. Supersede requires stable signed non-mutation/cleanup proof; late mutation never resurrects Bound.
- signature/key rotation/canonical payload tamper, repeat-read instability, external generation/revision/content/binding generation/receipt/enforcement mismatch, unsigned readback and static/scheduler-only protection all make Bound 0.
- gateway no-bypass Pending→Active and native Pending self-deny/external-version CAS are required. per-read Authority revoke/expiry/boundary drift, kill switch, inactive/releasing enforcement and missing/stale/mismatched attestation deny before payload.
- S1c.2b writes no proposal/eval/promotion/serving target state and admits no v6 evidence as apply, Bound or read authorization.

## 결과

S1c.2b는 adapter 편의 계층이 아니라 첫 real-source 경계다. 이 ADR이 구현되고 gate를 통과하기 전에는 fake-only S1c.2a가 source worker의 최대 capability이며, real source mutation·Bound·source read authorization은 계속 0이다.
