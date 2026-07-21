# ADR 0059 — 중앙 Source Binding Authorization Receipt와 v6 격리

- 상태: 채택(Accepted)
- 날짜: 2026-07-20
- 대체: ADR 0058의 v6 intent authorization/capability 발급 결정. ADR 0058의 source-free upstream 보존, Pending fencing, stable exact read-back, Bound 이력 불변 원칙은 유지한다.

## 맥락

ADR 0058 v6은 `SourceBindingAdapter`가 capability·boundary plan·drift authorization을 제공하는 형태였다. adapter의 공개 값 객체 또는 자기보고는 중앙 Authority가 발급한 권한 증명이 아니며, frozen Pydantic 값 객체의 exact type 검사도 위조를 막지 못한다. 저장된 digest나 서명 없는 receipt를 다시 model validate하는 것 역시 semantic tamper를 검출하지 못한다. 또한 source read가 계속 허용되는지와 intent 시점의 snapshot은 별 문제다.

따라서 현재 v6 intent/Pending은 source mutation, Bound, serving/read 공개를 여는 증거가 아니다. 이 행은 감사·조사 목적의 legacy-unverifiable evidence로만 보존한다.

## 결정

### 1. 권한은 중앙 Authority만 발급하고 검증한다

새 additive v7 source-binding 경계는 `SourceBindingAuthorizationAuthority`를 통해서만 intent authorization을 얻는다. Authority는 DB-time linearization에서 authenticated principal/action, current `ResourceRef`, 정책, trusted source integration registry, Artifact Revision, current Data Boundary Snapshot 및 Declassification Receipt를 서버 측으로 재구성·검증한다. adapter가 capability·classification·boundary·drift action을 선언하거나 caller가 receipt/plan/drift/capability를 command에 넣는 방식은 금지한다.

production UoW factory는 arbitrary Authority, key set, `current` callback 또는 exact concrete type을 받지 않는다. public `ProductionAuthorityComposition(...)` constructor도 없다. ADR 0049/0050의 `bootstrap_authorized_production()`만 canonical config·central policy/key/revocation/declassification/integration registries·authenticated-principal resolver를 검증하고 single-use `ProductionAuthorityCapability` 및 sealed wiring attestation을 만든다. bootstrap handle의 internal `open_source_binding_authority()`만 그 attestation의 exact composition identity·open lifecycle에 결박된 opaque `SourceBindingAuthorityCapability`을 mint한다. source-binding UoW factory는 이 capability를 받되, same sealed bootstrap의 DB/source wiring identity와 lifecycle이 맞는지 capability owner가 확인한 뒤 같은 composition-owned Authority로만 verify를 위임한다. close/revoke 뒤 capability는 fail-closed다.

테스트는 public `make_trusted_…(keys, current)`나 public composition constructor가 아니라 `test_support.bootstrap_test_production_authority(TestProductionBootstrapFixture)`를 쓴다. fixture는 strict immutable policy/key/revocation/declassification/integration/principal/source snapshot data만 받고 callback을 받지 않으며, production과 같은 bootstrap validation·wiring seal을 거친 handle에서 capability를 얻는다. UoW는 capability의 concrete type을 authorization 근거로 삼지 않고 매 operation의 typed resolution만 소비한다. 이는 trusted-process composition 경계다. 악성 in-process 코드가 private object를 반사·변조하는 것을 Python으로 막는다고 주장하지 않으며, 그 위협 모델에는 외부 Authority/HSM/process isolation이 추가로 필요하다.

Authority가 발급하는 immutable, signed `SourceBindingAuthorizationReceipt`의 canonical payload는 최소 다음을 결박한다.

- format version, receipt ID, issuer key ID, issued/expires DB time, Authority-signed intent semantic digest
- org/tenant ID와 구조화된 source `ResourceRef`
- expected source revision, artifact revision ID와 content digest
- data classification, boundary snapshot ref/digest
- declassification receipt ID·digest·expiry(있을 때)
- `reciprocal_review.source_bind` action과 authenticated principal/grant binding
- policy version/digest, trusted integration ID와 profile version/digest
- enforcement mode/plan digest, source drift action digest와 expiry

receipt signature는 Authority trusted issuer key registry로 검증한다. intent, worker apply, terminal Bound와 매 source read는 모두 `verify_current(receipt, purpose, db_now)`를 다시 통과해야 한다. 서명만 유효하거나 저장 필드가 같은 것은 current authorization이 아니다. policy/boundary/declassification/integration/revocation/expiry drift는 deny 또는 write 0이다.

### 2. 공개 DTO와 신뢰 capability를 분리한다

`SourceBindingAuthorizationReceiptEnvelope`는 persistence/transport용 strict canonical DTO일 수 있다. 그러나 domain operation이 받는 `VerifiedSourceBindingAuthorization`은 verifier만 만들 수 있는 opaque capability다. Python의 private 생성자나 `type(...) is ...`은 보안 경계가 아니므로, 사용 시마다 canonical payload digest, strict exact-key decoding, issuer signature, issuer trust registry 및 current Authority 검증을 수행한다. DB row·Pydantic 재구성만으로 opaque capability를 만들지 않는다.

### 3. port 책임을 분리한다

- `SourceBindingAuthorizationAuthority.issue_intent(...)`와 `.verify_current(...)`는 Authority-owned authorization port다.
- `TrustedSourceIntegrationRegistry.resolve(source_ref)`는 중앙 allowlist의 CAS/idempotency/fencing/exact-read-back/every-read enforcement profile만 반환한다.
- `SourceBindingExecutor`는 apply와 exact read-back observation만 수행한다. 권한이나 capability를 발급하지 않는다.
- gateway/native `SourceBoundaryEnforcer`는 protected source identity, revision/content digest, binding generation, active receipt ID를 가진 every-read attestation을 낸다. read gate는 payload 전 Authority current verify와 attestation exact match를 요구한다.

Pending/Releasing/Archived/unknown protection, missing/mismatched attestation, static ACL, scheduler, webhook, poller는 read authorization이 아니다. Bound는 fresh exact read-back, Active enforcement 및 current verification 뒤에만 가능하다.

### 3a. `verify_current`는 bool이 아닌 sealed typed resolution이다

`verify_current`의 최소 계약은 다음과 같다. `SourceBindingAuthorizationReceiptEnvelope`는 untrusted persistence/transport input이며, `ServerSourceBindingVerificationContext`는 production composition의 Authority가 server-side resolver로만 만든 opaque current context다. 외부 요청, adapter, DB row, 테스트 fixture가 context 또는 verified result를 직접 구성할 수 없다.

```text
SourceBindingOperation = IntentCreate | SourceApply | BoundTerminal | SourceRead

SourceResourceRef
  org_id, kind, source_id, owner_subject_id?       # canonical structured ref; string source_ref 금지

ServerSourceBindingVerificationContext (opaque)
  db_now
  principal binding + current verified grant binding
  action + SourceBindingOperation purpose
  SourceResourceRef + expected_source_revision
  artifact_revision_id + artifact_content_digest
  data_classification + boundary_snapshot_ref + boundary_digest
  verified declassification: receipt_id + receipt_digest + expires_at | absent
  current policy_version + policy_digest
  trusted integration profile ID/version/digest
  expected binding generation + required source read-back/attestation (BoundTerminal | SourceRead)

verify_current(untrusted_envelope, server_context)
  -> VerifiedSourceBindingAuthorization
   | SourceBindingAuthorizationDenied(reason_code)
   | SourceBindingAuthorizationUnavailable(reason_code)
```

Authority는 untrusted envelope의 strict exact-key decode와 canonical payload digest, issuer key trust/signature를 먼저 확인한 뒤, receipt의 모든 위 binding을 server context와 exact 비교한다. 특히 declassification은 ID만이 아니라 digest와 expiry를, policy는 version과 digest를, principal은 current authenticated principal 및 current verified grant binding을, operation은 action과 lifecycle purpose를 함께 비교한다. `BoundTerminal`/`SourceRead`는 expected source revision 및 fresh read-back/attestation의 structured source identity·revision/content digest·binding generation·active receipt ID도 exact match여야 한다. expiry는 `db_now`로만 판정한다.

`VerifiedSourceBindingAuthorization`은 verifier만 만들 수 있는 opaque capability이며 필요한 correlation-safe 값(receipt ID, payload digest, valid-until, binding generation)만 읽기 projection으로 낸다. `Denied`는 neutral/fail-closed reason code만, `Unavailable`은 retryable reason code만 낸다. 어느 resolution도 public envelope의 필드, `model_validate`, `model_copy`, `type` 검사, 또는 caller-supplied context로 만들어지지 않는다. intent/apply/terminal/read gate는 `Verified...`를 명시적으로 요구하고 다른 두 arm에서는 write 0 또는 deny한다.

### 4. canonical persistence와 tamper 처리

Authority issuance 전 UoW는 `SourceBindingIntentSemanticPreimage`를 canonical JSON으로 만든다. 이는 `org_id`, receipt/intent/audit/outbox/idempotency IDs, cycle/upstream kind+revision, binding generation, structured `SourceResourceRef`, expected source revision, artifact revision/content digest, classification, boundary snapshot ref/digest, declassification ID/digest/expiry, action/purpose, principal/grant binding, policy and integration profile version/digest, enforcement/drift plan digest를 exact-key로 포함한다. SHA-256인 `intent_semantic_digest`를 Authority issuance request에 넣고 Authority는 재구성한 current context와 비교한 뒤 그 digest를 receipt payload에 서명한다. receipt를 포함한 뒤 digest를 계산하는 순환은 허용하지 않는다.

v7 intent는 canonical preimage JSON/digest, canonical envelope bytes/JSON, payload SHA-256, signature, issuer key ID, receipt ID, structured `SourceResourceRef` fingerprint 및 모든 semantic binding field를 immutable audit/outbox evidence와 함께 저장한다. audit/outbox event payload도 각각 `kind`, intent semantic digest, receipt payload digest, receipt/audit/outbox IDs의 canonical exact-key preimage와 derived digest를 저장한다. open/replay validator는 **stored v7 upstream tuple을 권위 근거로 쓰지 않는다**. 먼저 validated v2/v5 ledger를 재조회해 `(org_id, cycle_id, upstream_kind, expected_upstream_revision)`의 exact current `BindingReady` upstream이 정확히 하나인지 확인하고, 그 authoritative tuple의 org/kind/revision/revision ID가 stored v7 tuple과 정확히 같은지 대조한다. 없거나 여러 개이거나 하나라도 다르면 fail-closed한다. 그 뒤에만 queried authoritative tuple과 persisted intent columns에서 preimage를 재구성해 stored canonical JSON/digest와 비교하고, receipt signature/trust와 receipt의 signed intent semantic digest, audit/outbox payload preimage와 derived digest/IDs/one-to-one FK, foreign aggregate/read-back consistency를 차례로 확인한다. command/audit/outbox digest를 함께 다시 계산하거나 stored upstream tuple까지 함께 바꿔도 authoritative v2/v5 query 또는 signed receipt semantic digest가 맞지 않으면 `source_binding_receipt_semantic_tamper`로 fail-closed한다. DB time은 canonical UTC milliseconds이며 expiry 판정의 기준은 Authority DB time이다.

오류는 cross-tenant 존재를 드러내지 않는 `not_found_or_denied`, retryable `source_binding_authority_unavailable`/`trusted_source_integration_unavailable`, fail-closed `source_binding_receipt_invalid|semantic_tamper|expired|revoked|policy_drift|boundary_drift|declassification_invalid|principal_or_grant_mismatch|action_or_purpose_mismatch|source_ref_mismatch|expected_source_revision_stale|untrusted_integration|enforcement_not_active|every_read_attestation_missing|read_attestation_mismatch`, concurrency `idempotency_payload_conflict|binding_generation_stale`로 구분한다.

### 5. migration compatibility

v1–v5 `BindingReady` 및 그 upstream evidence는 immutable source-free evidence로 그대로 보존한다. existing v6 intent/Pending/attestation은 자동 서명·backfill·replay·Bound 전환·read 공개를 하지 않는 legacy-unverifiable evidence다. source owner의 human-only reconciliation과 fencing proof 없이는 Superseded조차 자동 기록하지 않는다. 새 v7 cycle은 새 Authority receipt로만 시작하며 v6 receipt를 새 권한으로 승격하지 않는다.

## 구현 게이트 테스트

- production UoW factory가 arbitrary Authority/key registry/current lambda/public composition constructor/concrete-type assertion을 받지 않고 `bootstrap_authorized_production()` sealed attestation에서 나온 live capability만 받는지 검증한다. close/revoke·wiring/DB/source identity mismatch 뒤에는 capability가 fail-closed해야 한다. test fixture도 strict snapshot data로 bootstrap한 test handle capability만 써서 signed envelope/typed resolution을 얻고 verified capability를 직접 만들지 않는다.
- valid Authority-issued envelope와 Authority-owned exact context만 `VerifiedSourceBindingAuthorization`을 돌려준다. public DTO 직접 생성·복사·재직렬화, forged signature/key ID, canonical-key/bytes/digest tamper는 모두 `Denied`이고 write 0이다.
- org/structured `SourceResourceRef`, expected source revision, artifact revision/content digest, classification/boundary snapshot/digest, declassification ID/digest/expiry, policy version/digest, principal/grant, action/purpose 중 한 필드라도 달라지면 `Denied`다. source 존재 여부는 다른 tenant에 노출하지 않는다.
- Authority DB time의 issue/expiry 양끝, revoked/changed policy·boundary·declassification·integration profile은 `Denied`; resolver/key/source 장애는 `Unavailable`이며 재시도 전 write 0이다.
- `BoundTerminal`과 `SourceRead`는 fresh exact read-back/attestation의 source identity·revision/content·generation·active receipt ID가 모두 같은 경우만 Verified다. Pending/Releasing/unknown/static ACL과 missing/stale/mismatched attestation은 deny다.
- intent/apply/terminal/read gate가 bool, public envelope, caller-created context를 받아들이지 않고 `Verified...` arm만 받는지, v6 row가 v7 verified result 또는 Bound/read 공개로 승격되지 않는지를 결정론 composition-backed Authority registry로 검증한다.
- command digest·audit/outbox derived digest를 함께 바꾼 coordinated DB tamper도 validator가 authoritative v2/v5 exact BindingReady re-query와 reconstructed semantic preimage, Authority-signed `intent_semantic_digest` 불일치로 닫는지 검증한다. stored v7 upstream tuple의 단독·동시 변경, v2/v5 upstream 없음/복수/다른 kind·revision·revision ID, ID/idempotency/cycle/resource/boundary/principal/policy/plan 변경, audit/outbox payload/FK/1:1 관계 변경도 write 0이어야 한다.

## 결과

Authority centralization(ADR 0004·0050)과 source-boundary continuous enforcement(ADR 0058의 유지 원칙)을 함께 보존한다. v7 구현 전 실제 source binding write와 source read 공개는 계속 0이다.
