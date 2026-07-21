# ADR 0047 — 상호 검토와 통제된 개선

- 상태: 채택(Accepted, 2026-07-13)
- 결정 계기: 사용자가 제품의 추가 핵심 목표를 "사람이 만든 것은 AI가 검토하고,
  AI가 만든 것은 사람이 검토하며, 그 결과로 계속 발전하는 시스템"으로 확정했다.
- 계보: ADR 0003·0019·0025·0029·0033·0035·0041·0042~0046
- 구현 상태: 설계와 Phase 18 작업 경계만 채택했다. 공통 산출물 계보, 검토 원장,
  개선 후보, 승격·롤백 코드는 아직 없다. P17.8 RBAC·P17.9 durable workflow 전에는
  Fake·synthetic shadow만 허용한다. 두 게이트 뒤에는 실제 데이터의 durable review와 shadow
  eval까지 열 수 있지만, P17.11 평가·P17.12 통합 리허설·P17.13 운영 게이트까지 통과해야
  canary·사람 승격·serving target state write를 열 수 있다.

## 맥락

현재 저장소에는 상호 검토의 조각이 이미 있다.

- `StageReview`는 AI가 만든 OKF 초안을 Owner가 승인·편집·거절한다.
- `ApprovalItem`은 AI 답변 후보가 최종 답이 되기 전에 인증된 사람의 처분을 받는다.
- `BackupReview`, `CorrectionEvent`, `AnswerFeedback`은 발송 뒤 검토와 교정을 남긴다.
- `ReevalItem`은 지식 변경으로 오래된 답과 판례를 다시 사람에게 보낸다.
- 골든셋과 eval runner는 분류·라우팅·선택적 답변 품질을 측정한다.

하지만 이 기능들은 서로 다른 목적의 독립 상태기계다. 공통 revision provenance,
작성 주체 교차 검토, 검토 결과에서 개선 후보로 이어지는 계보, 독립 평가, 활성 버전 승격,
롤백이 없다. 사람이 작성한 산출물을 AI가 근거와 함께 검토하는 경로도 없다.

여기서 "자기발전"을 모델 가중치나 운영 상태의 자율 변경으로 해석하면 기존 불변식과
충돌한다. AI가 자기 출력을 정답으로 삼으면 eval은 자기확증에 빠지고, AI가 Authority나
RBAC를 바꾸면 중앙 권한 원칙이 무너진다. 승격 전 사람 검토를 건너뛰면 Approval 경계도
무의미해진다.

## 결정 1 — 새 bounded context를 둔다

새 경계의 이름은 **Reciprocal Review & Governed Improvement(상호 검토·통제된 개선)**다.
Question Resolution 뒤쪽의 거버넌스 계층이며, 질문 수명주기나 기존 승인 상태를 소유하지
않는다.

이 문맥에서 자기발전은 다음 한 방향 흐름을 뜻한다.

```text
불변 산출물 revision
  → 작성 주체 교차 검토
  → 사람이 수용한 finding
  → 불변 개선 후보
  → 독립 held-out 평가
  → 권한 있는 사람의 승격
  → serving target state CAS
  → 필요할 때 versioned rollback
```

AI 검토와 운영 피드백은 후보를 만들 수 있을 뿐 활성 상태를 직접 바꾸지 않는다.

## 결정 2 — 산출물 revision과 작성 계보를 먼저 고정한다

미래 도메인 shape는 다음 값을 기준으로 한다. 구체 필드는 구현 슬라이스에서 frozen
pydantic 값 객체로 확정한다.

```python
class ArtifactRevision(FrozenDto):
    org_id: str
    artifact_id: str
    revision_id: str
    revision_no: int
    parent_revision_id: str | None
    kind: ArtifactKind
    content_ref: str
    content_sha256: str
    provenance: AuthorshipProvenance
    data_classification: DataClassification
    data_boundary_snapshot_ref: str
    data_boundary_digest: str
    declassification_receipt_id: str | None
    request_id: str | None
    record_id: str | None
    created_at: datetime
    schema_version: int
```

`content_ref`는 기존 도메인의 **불변 버전**을 가리키는 주소다. git commit·문서 version·
append-only record처럼 같은 ref가 보존 기간 동안 같은 bytes를 돌려줘야 한다. `latest`나 mutable
URL은 허용하지 않는다. 새 검토 원장에 지식 본문이나 답변 본문을 복제하지 않되,
`content_sha256`과 상위 revision으로 읽은 bytes와 계보를 검증한다. 원 도메인이 버전 보존을
보장하지 못하면 공통 원장이 아니라 원 도메인의 보호된 immutable snapshot 저장소에 먼저
고정한다. 삭제·보존·legal hold가 끝난 ref는 승격·재평가 근거로 쓸 수 없다.

child의 effective data boundary는 caller 입력이 아니라 parent와 이번 revision이 참조한 모든
source에서 보수적으로 계산한다. `data_classification`은 가장 높은 등급, ACL은 허용 범위의 교집합,
purpose·region·retention은 가장 제한적인 조건을 상속한다. 이 전체를 immutable
`data_boundary_snapshot_ref`와 digest로 고정한다. snapshot이 없거나 parent보다 느슨한 revision은
등록하지 않는다. 합법적인 redaction·declassification은
기존 revision을 고치지 않고, data owner/security authority, old/new boundary, exact source·
redaction digest, policy·법적 근거, 유효기간을 결박한 immutable `DeclassificationReceipt`를 가진
새 revision으로만 만든다. AI reviewer·proposal generator는 이 receipt를 발행할 수 없다. 저장
snapshot과 별개로 content read·review·promotion 시점의 현재 ACL·purpose도 다시 확인한다.

AI review로 내용을 내보내기 전, `BindingPending` 전, promotion·package handoff 직전에는
`DeclassificationReceipt`의 issuer 권한, policy version, source/new boundary scope, expiry를 다시
검증한다. 하나라도 유효하지 않으면 model/source/serving/package write는 0이다. 진행 중 cycle은
결정 4의 공통 drift 계약을 따른다. BindingPending 전이면
`Superseded(reason=declassification_invalid)`로 닫고, BindingPending이면 native/gateway로 노출을
막은 채 exact source terminal까지 settle한다. 기존 `Bound` 이력은 고치지 않는다. 같은 revision을
낮은 boundary로 다시 열 수 없으며 data owner가 원래의 보수적 boundary를 상속한 새 revision을
등록해야 한다.

만료 시각 뒤 사람 처분을 기다리는 동안 노출을 계속하지 않는다. expiry뿐 아니라 source ACL·
purpose·classification·region·retention version의 강화나 철회도 serving 중 계속 집행한다.
in-process serving path는 매 요청에서 authoritative current boundary version과 receipt expiry를
검사해 즉시 deny한다. 외부 target의 기본 허용 경로는 둘뿐이다.

1. 우회할 수 없는 platform access gateway가 모든 read에서 authoritative IdP/source의 current
   boundary version과 unforgeable current serving revision·activation/binding generation을 읽고,
   exact current Active reference/final receipt와 대조해 불일치하면 즉시 deny한다.
2. target-native authorization이 exact serving revision·activation/binding generation과 같은
   authoritative grant에 원자 결박되고, target/source가 모든 read에서 revision/generation·grant·
   lease 불일치나 revocation을 자체 fail-close하며 exact enforced state를 read-back한다.

promotion 시점에 복사한 static native ACL만으로는 immediate revocation을 보장하지 못하므로 기본
정책에서 허용하지 않는다. 데이터 분류와 target 정책이 bounded staleness를 명시적으로 허용할
때만 `ServingBoundaryLease(boundary_version, valid_until, max_revocation_lag, policy_authorization)`를
쓸 수 있다. target은 lease expiry 뒤 모든 read를 자체 deny해야 하고,
`valid_until <= issued_at + max_revocation_lag`와, declassification expiry가 있으면
`valid_until <= declassification_expiry`도 강제한다. secret·
restricted, tenant isolation, legal hold, Authority/RBAC 경계에는 이 예외를 허용하지 않는다.
갱신은 authoritative current boundary를 다시 읽고 exact same immutable revision boundary
version/digest일 때만 external-version CAS한 뒤 exact read-back할 수 있다. 더 제한적인 boundary도
immutable revision의 snapshot과 달라진 drift이므로 renewal 0·새 revision 경로다. webhook·
scheduler·poller는 drift를
빨리 발견하는 보조 수단일 뿐 continuous enforcement receipt가 아니다. 이 세 경로 중 하나도
없으면 revision을 그 target에 승격할 수 없다.

in-process activation은 authoritative boundary source/version, per-read access-gate config digest,
fail-closed health policy와 optional declassification expiry를 결박한
`InProcessBoundaryEnforcementReceipt`를 active-state CAS와 함께 쓴다. serving code가 이 receipt와
current boundary를 매 read에서 검증하지 않는 composition은 enable하지 않는다.

expiring declassification revision을 target에 RevisionActivation 또는 external ServingRevision
adoption할 때 권한 있는 사람은 exact target·revision·boundary·
expiry·조치·policy를 결박한
target-only `ScheduledBoundaryExpiryAction`도 미리 승인한다. scheduler는 충분한 safety margin 전에 정상
quarantine/deactivate를 시작하는 상태 수렴 보조 장치다. expiry까지 exact read-back을 얻지
못해도 위 native TTL 또는 no-bypass gateway가 노출을 차단한다.

모든 external serving activation과 source binding은 사람
`BoundaryDriftActionAuthorization`도 미리 가진다. 공통으로 exact revision·current boundary
version/digest·drift predicate·issuer principal/role·policy·authorization horizon·idempotency를
결박하고 다음 두 arm을 닫는다.

```text
TargetBoundaryDriftActionAuthorization(
  target/environment, action: Quarantine | Deactivate, expected slot state/epoch)
SourceBoundaryDriftActionAuthorization(
  source aggregate/ref, expected source revision,
  action: SourceDenyReads | SourceUnpublish, adapter/schema version)
```

AI principal은 이를 발행·변경할 수 없다. target arm은 RevisionActivation enforcement plan·
desired/epoch CAS 또는 ServingRevision adoption plan·slot reservation과 같은 transaction에 넣고,
drift handler는 정상 shared slot·epoch saga request만 발행한다.
source arm은 binding intent·`BindingReady → BindingPending` CAS와 같은 transaction에 넣고,
source가 소유한 expected-revision CAS·semantic idempotency·worker fence·stable exact read-back
adapter만 호출한다. common ledger가 source 상태를 직접 쓰거나 target slot으로 가장하지 않는다.
authorization이 없거나 만료됐으면 gateway/dynamic/lease의 serving read deny와 escalation은
유지하되 target/source convergence write는 0이다.

외부 target의 요청 전 증거와 적용 뒤 증거를 섞지 않는다.
`BoundaryEnforcementPlan = NativeBoundaryEnforcementIntent | GatewayEnforcementIntent`이고,
최종 `BoundaryEnforcementReceipt = NativeBoundaryEnforcementReceipt |
GatewayEnforcementReceipt`다.
gateway arm도 request 시점에는 receipt를 선기록하지 않는다. `GatewayEnforcementIntent`는
gateway route ID, expected route/config version·digest, authoritative IdP/source binding과 current
boundary version, no-bypass topology digest, fail-closed health policy와 apply_before를 결박한다.
consumer는 외부 호출 직전에 intent와 current authorization을, terminal 직전에는 실제 gateway
route/config/health와 current boundary를 fresh exact read-back한다. terminal receipt는 그 current
version·health와 해당 target activation generation 또는 source binding generation을 함께 결박한다.
mismatch면 Applied/Bound는 0이고 boundary drift의 fail-closed 경로다.

gateway 보호 주체와 참조는 다음 닫힌 합과 exact key를 쓴다.

```text
GatewayReferenceSubject =
  TargetGatewaySubject(target slot, target operation 또는 adoption-intent ID,
                       reserved target epoch)
  | SourceGatewaySubject(source aggregate/ref, source binding intent ID,
                         expected source revision)

GatewayRouteReference key =
  (org, subject, protected revision ID, route/config generation)
GatewayRouteReference = Pending | Active | Releasing | Archived
```

subject에서 intent ID를 뺀 `(org, target slot 또는 source aggregate/ref, route ID)`가
`GatewayProtectionScope`다. 같은 scope에는 current serving identity와 exact route/config generation을
보호하는 Active reference가 최대 하나여야 하며 partial unique index와 expected reference revision
CAS로 강제한다. 서로 다른 Pending winner가 terminal을 경합하거나 Active가 둘 이상 관측되면
allow를 고르지 않고 read deny·`GatewayRouteReferenceConflict`다. replacement terminal은 new
Pending→Active와 old Active→Releasing을 같은 transaction에서 처리한다.

target RevisionActivation은 UoW12가 `GatewayEnforcementIntent`와 Pending을 함께 만들고, UoW13 성공
terminal이 fresh exact read-back과 `GatewayEnforcementReceipt`를 쓰면서 같은 reference를 Active로
CAS한다. 외부 `ServingRevision` adoption도 아래의 durable request transaction에서 Pending을 먼저
만든 뒤 terminal transaction에서 같은 규칙을 쓴다. A→B 정상 Promotion/Rollback/adoption이면 B의
final boundary-enforcement receipt와 target read-back을 확정한 같은 transaction에서 A의 보호
Active reference를 Releasing으로 옮긴다.

source gateway binding은 UoW7이 `SourceBoundaryEnforcementPlan`과 Pending을 함께 만든다. UoW8이
fresh source+gateway stable exact read-back으로 Bound를 확정하면서 source revision이 읽기/게시
상태면 같은 reference를 Active로 CAS하고, 새 source revision으로 교체했다면 기존 보호 Active를
Releasing으로 옮긴다. Bound outcome이 source를 비서빙/deny 상태로 남기거나 BindingFailure가 난
경우에는 worker/attempt를 먼저 fence하고 stable exact read-back이 exact protected revision의
미서빙을 증명해야 Pending을 Releasing으로 옮길 수 있다. `SourceDenyReads | SourceUnpublish`도
같은 source-owned expected-revision/idempotent/fenced read-back 뒤 Active를 Releasing으로 옮긴다.

failed·denied·superseded target operation/adoption의 Pending도 worker/attempt를 fence하고 stable exact
read-back이 target의 exact protected revision 미서빙을 증명한 뒤에만 Releasing으로 간다.
Active/Releasing reference는 subject가 가리킨 target 또는 source의 fresh stable read-back이 exact
protected revision 미서빙을 증명하고, replacement final boundary-enforcement receipt 또는
fail-closed kill/deny 보호가 이미 확정된 뒤 별 cleanup CAS로 Archived가 된다. late mutation
가능성, unrecognized read-back, replacement 보호 공백이 하나라도 있으면 release write는 0이고
non-Archived reference를 안전한 leak으로 남긴다. Archived reference는 다시 활성화하지 않는다.

어느 subject에서든 Pending·Active·Releasing reference가 하나라도 있는 route/config는 공통
reference-fenced CAS로 삭제·완화할 수 없다. 더 제한적인 변경이나 위 replacement/kill/deny saga만
허용한다. gateway 자체도 expected config version이 달라지거나 authoritative boundary를 읽지
못하면 모든 read를 deny한다. 이 공통 reference fence와 no-bypass topology를 제공하지 못하는
gateway는 target 또는 source enforcement arm으로 enable하지 않는다.
native target 경로는 아직 적용 전이므로 receipt를 선기록하지 않고, exact revision·boundary·
optional declassification expiry·expected external version·target capability와 다음 닫힌 합을
결박한 `NativeBoundaryEnforcementIntent`만 둔다.

```text
NativeContinuousMode =
  DynamicAuthoritativeGrantIntent(authority_ref, boundary_version, fail_closed_config_digest)
  | ServingBoundaryLeaseIntent(boundary_version, valid_until,
                               max_revocation_lag, policy_authorization)

NativeContinuousReceipt =
  DynamicAuthoritativeGrantReceipt(enforced_boundary_version,
                                    authority_ref, exact_readback_digest)
  | ServingBoundaryLeaseReceipt(enforced_boundary_version, valid_until,
                                max_revocation_lag, exact_readback_digest)
```

final `NativeBoundaryEnforcementReceipt`는 exact protected revision/content digest·activation 또는
binding generation·boundary·optional expiry·external version·TTL/ACL과
`NativeContinuousReceipt`를 결박한다. plan과, expiring target revision이면 durable scheduled
action은 RevisionActivation request의 serving/desired state CAS 또는 external ServingRevision
adoption의 slot reservation과 **같은 transaction**에서 확정해 뒤늦게 등록하는 crash window를
허용하지 않는다.

native target/source 자체의 serving binding도 닫힌 상태다.

```text
NativeServingBinding =
  Pending(protected revision/content digest, activation/binding generation, plan digest)
  | Active(protected revision/content digest, activation/binding generation,
           enforced boundary/mode digest, valid_until?)
  | Denied(previous protected revision?, reason)
```

native consumer의 첫 conditional CAS는 expected external version으로 revision 활성화/source publish,
native TTL/ACL과 dynamic authoritative-grant binding 또는 boundary lease를 exact
`NativeServingBinding(Pending)`과 원자 적용한다. Pending은 모든 read를 자체 deny한다. consumer가
current authorization·boundary·non-superseded intent를 다시 확인한 뒤에만 같은 exact external
version/generation의 Pending을 Active로 conditional CAS하고, revision/content digest·TTL/ACL·
enforced boundary·continuous mode·Active generation을 함께 stable read-back한다. 그 뒤 DB terminal
transaction에서만 `NativeBoundaryEnforcementReceipt`와 해당 RevisionActivation/Adoption Applied를
함께 확정한다. source native binding도 UoW7 intent 뒤 같은 Pending→Active·stable read-back을 거쳐
UoW8의 `SourceBoundaryEnforcementReceipt`와 Bound를 확정한다. revision/content와 continuous
enforcement의 원자 Pending 설치, per-read self-deny, conditional Active 전이 또는 exact read-back을
지원하지 않는 target/source는 native boundary-enforcement arm으로 enable하지 않는다.

gateway와 native에 공통으로 다음 serving identity fail-closed 규칙을 적용한다.

```text
ServingIdentityAttestation =
  TargetServingAttestation(target slot, protected revision/content digest,
                           activation generation, external version)
  | SourceServingAttestation(source aggregate/ref, protected revision/content digest,
                             binding generation, source revision/version)
```

target/source는 content mutation마다 바뀌고 위조·재사용할 수 없는 revision/content digest와
monotonic generation을 every-read 경로에 제공해야 한다. gateway는 매 read마다 이 attestation을
얻어 authoritative current boundary, exact route/config generation, current
`GatewayRouteReference(Active)`와 그 final enforcement receipt의 protected revision/content digest·
activation/binding generation에 모두 일치할 때만 allow한다. matching reference가 Pending·
Releasing·Archived뿐이거나 final receipt가 없거나 만료·철회됐거나, attestation이 missing·unknown·
mismatch면 read는 0이다. target/source가 no-bypass per-read attestation을 제공하지 못하거나 mutation이
같은 generation을 재사용할 수 있으면 gateway arm으로 enable하지 않는다.

native target/source는 매 read마다 current served revision/content digest·activation/binding
generation을 자신의 exact Active native binding과 대조한다. Pending/Denied, binding·grant·lease
부재, generation/revision mismatch, boundary/expiry/revocation mismatch는 모두 자체 deny한다.
따라서 external activation/adoption/source publish의 content commit 뒤 final Active 전이 전 Pending
창은 짧은 가용성 중단으로 남고 노출 창이 아니다. gateway에서도 새 content attestation에 matching
Active reference가 생기기 전에는 같은 방식으로 deny한다. out-of-band로 과거 revision이 돌아오거나
Archived/Releasing reference만 남은 revision이 재등장해도 read는 즉시 deny하며, background drift
detection은 상태 수렴과 감사의 보조 수단일 뿐 노출 차단 수단이 아니다.

scheduler는 AI 판단이 아니라 사전에 승인된 중앙 정책을 결정론적으로
집행한다. 별 target CAS 포트를 호출하지 않고,
preapproved action에서 정상
`QuarantineRequested | DeactivateRequested`를 발행해 shared slot·epoch, expected state,
desired/observed saga, outbox, adapter와 exact read-back 계약을 그대로 탄다. stale expected
state면 write 0 receipt를 남긴다. expiry 사건은 사람 governance owner에게도 즉시 escalation한다.
AI가 자동으로 boundary를 낮추거나 kill-switch를 승인하지 않는다.

lease mode의 initial plan은 exact target·revision·boundary version, max lag, renewal horizon,
optional declassification expiry와 policy digest를 가진 사람
`BoundaryLeaseRenewalAuthorization`도 결박한다. renewal scheduler는 direct target write를 하지
않는다. current lease의 fail-closed margin 전에 authoritative boundary를 다시 읽고, exact same
immutable revision boundary version/digest이며 authorization horizon 안일 때만 후보를 만든다.
각 request UoW는 current policy revision/digest, classification의 lease eligibility,
policy가 현재 허용한 max revocation lag, optional declassification expiry와 issuer authorization
validity를 다시 conditional-check한다. initial plan의 expected policy revision과 하나라도 다르면
renewal write는 0이다. 모두 유효할 때만
`BoundaryLeaseRenewalRequested`를 shared target slot에 만든다. request는 current serving/desired
state·slot epoch, current/proposed lease, source boundary version, expected external version,
idempotency key를 결박하고 slot epoch를 증가·예약해 non-superseded in-flight를 하나로 만든 뒤
desired boundary-enforcement metadata와 outbox를 한 transaction으로 CAS한다. consumer는 target lease만 conditional
external-version CAS하고 exact read-back 뒤
`BoundaryLeaseRenewalApplied | BoundaryLeaseRenewalDenied | BoundaryLeaseRenewalSuperseded`를
닫는다. 사용 중인 revision·ACL을 바꾸지 않는다.

boundary/policy mismatch, classification ineligibility, authorization revoke/expiry, DriftOpen, 다른
in-flight operation, expiry 뒤 요청은 renewal write 0이다. `BoundaryDriftObserved` 또는
`GovernanceDriftObserved(kind=boundary_lease_policy)`·audit·escalation을 남기고 preauthorized urgent normal
Quarantine/Deactivate를 시작한다. 기존 lease 자체가 expiry에 read를 deny하므로 worker 장애가
노출 연장으로 바뀌지 않고 `valid_until`까지만 LeaseActive로 표시된다. renewal은 target operation priority에서 kill-switch 아래이고,
higher-priority Quarantine/Deactivate가 pending renewal을 supersede한다. source adapter가 lease를
쓰는 경우에도 같은 durable intent/outbox, external-version fence, exact read-back terminal 계약을
제공하지 못하면 binding 대상으로 enable하지 않는다.

모든 external source binding intent는 exact current boundary version/digest와 continuous mode를
source command에 포함하고, expiring declassification이면 expiry를 추가로 결박한다. source
publish/read 경로도 no-bypass gateway, authoritative dynamic native
authorization, 정책이 허용한 fail-closed `ServingBoundaryLease` 중 하나를 commit 시각에 조건부
적용하고 exact enforced boundary version을 read-back해야 한다. static copied ACL이나 scheduler만
가능하면 `BindingPending`으로 전이하지 않는다. source commit이 expiry를 넘기거나 terminal
read-back에서 continuous enforcement가 확인되지 않으면 `Bound` write는 0이고
위 `DataBoundaryInvalidationReceipt`/fail-closed 경로를 탄다.
`SourceBoundaryEnforcementPlan`은 source aggregate/ref·expected source revision과 위 exact
gateway/native continuous intent를, final `SourceBoundaryEnforcementReceipt`는 source stable
read-back과 enforced boundary version/mode를 결박한다. UoW8의 source `BindingReceipt`는 이 final
receipt를 포함해야 하며 common ledger가 대신 합성하지 않는다.
gateway arm이면 UoW7이 exact `SourceGatewaySubject`의 `GatewayRouteReference(Pending)`을 plan·
binding intent와 같은 transaction에서 만들고, UoW8은 위 공통 lifecycle에 따라 Bound와
Pending→Active 또는 verified non-serving의 Pending→Releasing을 함께 확정한다. BindingFailure,
source replacement, `SourceDenyReads | SourceUnpublish` 뒤 reference 정리도 source stable read-back과
replacement/deny 보호가 확인되기 전에는 Archived write가 0이다. source reference를 보지 않는
gateway route/config 완화 CAS는 허용하지 않는다.
expiring source binding의 `SourceBoundaryDriftActionAuthorization`은
`predicate=declassification_expiry`와 exact `SourceDenyReads | SourceUnpublish` action도 UoW7에서
함께 결박한다. source expiry convergence는 target-only Scheduled action을 가장하지 않는다.

serving 중 authoritative source boundary version이 바뀌면 immutable
`BoundaryDriftObserved(old/new version·digest, source ref, observed_at, target slots)`와
`DataBoundaryInvalidationReceipt(reason=source_boundary_drift)`를 남긴다. in-process·gateway·
dynamic-native 경로는 enforced version이 current source version/digest와 일치하지 않는 모든 read를
즉시 deny한다. bounded lease 경로만 승인된 `valid_until`까지 `LeaseActive`로 표시할 수 있고 갱신은
0이다. platform이 외부 target의 현재 serving/authorization 상태를 응답할 때도
`Current | LeaseActive(valid_until) | Denied`를 구분하며 stale receipt를 Current로 표시하지 않는다.
동시에 affected revision의 새 binding/promotion/rollback/adopt/compensation/retry/package write를
0으로 막고, 사전 승인된 shared slot·epoch saga로 urgent Quarantine/Deactivate를 시작해 audit·
escalation한다. 정상 수렴 뒤에도 immutable old revision을 다시 열지 않으며 authoritative current
boundary를 상속한 새 ArtifactRevision으로만 재개한다. webhook·scheduler 실패는 strict
gateway/dynamic 경로의 deny나 bounded lease 자체 expiry를 약화시키지 못한다.

작성 기원은 호출자가 `human | ai | mixed` 문자열로 직접 지정하지 않는다. `ArtifactRevision`의
`AuthorshipProvenance`에는 등록 서비스가 검증한
`HumanPrincipal | ModelExecution | DeterministicTransform | ImportedUnknown` 생성 사건과 그
불변 참조만 저장한다. 그 사건 집합과 append-only `ProvenanceResolutionReceipt`에서
`EffectiveAuthorshipProvenance = HumanAuthorship | AiAuthorship | MixedAuthorship |
UnknownAuthorship`을 파생한다.
여기서 실행 요청자와 내용 기여자를 구분한다. 모델 실행을 시작한 사람은 감사 actor이지만,
그 사실만으로 사람 내용 기여자가 되지는 않는다.

- `HumanAuthorship` — 내용 기여자가 인증된 `HumanPrincipal`뿐이다.
- `AiAuthorship` — 내용 기여자가 검증된 `ModelExecution`뿐이다. 실행 요청자는 별 audit 필드다.
- `MixedAuthorship` — 사람과 모델 내용 기여자가 각각 한 명 이상이다. 사람이 조금 편집하거나
  승인해도 AI 계보를 지우지 않는다.
- `UnknownAuthorship` — 아직 유효한 resolution receipt가 없는 `ImportedUnknown`이 있거나 내용
  계보를 완전히 증명하지 못했다. 사람 binding 검토만으로는 해소되지 않으며, effective
  provenance가 검증될 때까지 승격할 수 없다.
- `DeterministicTransform`은 상위 revision의 기원을 그대로 보존한다.

child revision의 저장 계보는 항상 `parent lineage event refs ∪ 이번 revision의 authenticated
content event refs`다. 사람이 AI parent를 고치거나 AI가 사람 parent를 고치면 effective
provenance는 `MixedAuthorship`이며, 마지막 편집자만 보고 기원을 낮출 수 없다. 이전 내용을 쓰지
않고 완전히 새로 만든 산출물이라면 parent를 잇지 않는 새 lineage로 등록해야 한다.

`ProvenanceResolutionReceipt`는 기존 revision을 수정하지 않는다. exact org·revision ID와
unknown event ID,
검증해 낸 `HumanPrincipal | ModelExecution` 사건, issuer principal·role snapshot, policy version,
source evidence ref·digest, 발행 시각, idempotency key를 결박해 append한다. 같은 unknown event에
서로 다른 결론을 내는 receipt는 `ProvenanceResolutionConflict`로 거부한다. 잘못된 resolution은
덮어쓰지 않고, 권한·사유·policy version·원 receipt를 결박한
`ProvenanceResolutionRevocationReceipt`로 먼저 철회한 뒤 새 증거로 다시 해소한다. effective
provenance는 특정 ledger sequence와 resolution-policy version을 기준으로 유효하고 철회되지
않은 receipt만 반영해 계산하고 digest를 남긴다. receipt의 효력 범위는 exact revision 하나다.
parent resolution이 기존 child의 effective provenance를 조용히 바꾸지 않으며, child에도 같은
증거를 쓰려면 child-scoped resolution receipt를 별도로 발행해야 한다. 새 resolution·revocation으로
digest가 바뀌면 receipt append와 expected previous digest CAS를 원자화하고 결정 4의 공통
`GovernanceDriftObserved` 계약을 따른다. BindingPending 전 nonterminal cycle은 `Superseded`와
새 cycle·requirement·assignee/SLA를 같은 transaction에서 만들고, BindingPending이면 source
settle 뒤 `Bound` terminal transaction에서 새 historical cycle을 연다. `BindingFailure`면 결정
5의 exact `SourceReconciliationReceipt` 없이는 같은 revision의 새 cycle을 열지 않는다. 이미
`Bound`인 이력은 고치지 않고 promotion write를 0으로 막은 뒤 새 historical cycle을 같은
transaction에서 연다.
따라서 원 `ImportedUnknown` 사건은 감사 계보에서 사라지지 않으면서도 검증된 보강 결과를
정책적으로 사용할 수 있다.
resolution·revocation issuer는 provenance-curator 권한을 가진 인증된 사람이어야 하며 AI
principal은 두 receipt를 발행할 수 없다.

`ModelExecution` receipt는 provider·model·immutable snapshot, deployment, prompt template·digest,
tool allowlist·호출 digest, retrieval snapshot, sampling 설정, 입력·출력 digest, runtime version을
보존한다. 출처를 증명하지 못하는 legacy 산출물은 승격할 수 없다.

## 결정 3 — 작성 주체 교차 검토와 사람의 binding 권한을 분리한다

- 사람이 만든 revision은 AI 자문 검토 영수증이 있어야 사람 처분 단계로 간다. finding이
  0건이어도 model·prompt·rubric·입력 digest가 결박된 signed empty batch가 필요하다.
- AI 또는 mixed revision은 인증된 사람의 binding 검토가 필수다. 추가 AI 검토는 이 조건을
  대신하지 못한다.
- AI finding에는 `approve`, `accept`, `promote` 필드를 두지 않는다. AI는 근거가 붙은
  `ReviewFinding`만 만든다.
- `AcceptFinding | RejectFinding | DeferFinding`과
  `ApproveRevision | RequestChanges | RejectRevision`은 사람 처분이다. blocking finding이
  해결되지 않은 상태에서는 승인할 수 없다. `DeferFinding`은 terminal이 아니며 담당자,
  `due_at`, SLA를 반드시 가진다.
- 수용된 finding은 `ImprovementProposal | FindingRiskAcceptance | FindingPolicyException` 중
  하나와 정확히
  연결돼야 한다. `ImprovementProposal`로 닫힌 blocking finding은 새 candidate revision에서
  해소될 때까지 현재 revision 승인을 막는다. `FindingRiskAcceptance`·`FindingPolicyException`은
  issuer principal·role snapshot·policy version, 해당 severity를 수용할 권한, 사유, 범위,
  만료시각을 갖고 정책이 허용할 때만 blocking을 해소한다.
  DLP·tenant isolation·Authority·RBAC·secret exposure·hard safety invariant처럼 policy가
  non-exceptionable로 지정한 finding은 두 receipt로도 닫을 수 없다.
  finding별 처분과 revision 전체 처분은 별개다. 중요 finding이 남아 있는데 일부 finding만
  처리했다는 이유로 revision을 승인할 수 없다.
- production 기본값은 binding reviewer가 모든 human contributor와 달라야 한다. 고위험
  revision은 reviewer와 promoter도 분리한다. MVP에는 직무 분리 면제 기능을 두지 않는다.
- AI reviewer는 저자와 다른 deployment·rubric을 기본으로 한다. 같은 모델의 검토는 자문
  증거가 될 수 있지만 품질 독립성 기준을 충족하지 못한다.
- 사람 작성 revision의 AI 검토가 실패하면 사람이 승인해도 AI 검토 완료로 기록하지 않는다.
  우회가 필요하면 issuer principal·role snapshot·policy version·대상 requirement·사유·범위·
  만료시각을 결박한 별 `ReviewRequirementWaiver`를 남긴다. finding 처분 예외와 같은 타입을
  쓰지 않는다.
- waiver는 policy가 `waivable`로 표시한 자문 requirement에만 적용한다. AI·mixed revision의
  사람 binding, 사람 promoter, contributor/reviewer 독립성, DLP·data-boundary, 독립 evaluation,
  hard safety invariant는 non-waivable이며 receipt가 있어도 write 0이다.
- `ReviewRequirementWaiver | FindingRiskAcceptance | FindingPolicyException`의 issuer는 해당
  범위 권한을 가진 인증된 사람이어야 한다. AI principal은 이 receipt를 발행·철회할 수 없다.

`ImprovementProposal`은 accepted finding 근거를 재사용할 수 없다. proposal 생성 transaction은
모든 basis를 canonical closure ID 순서로 잠그고, 각 finding/disposition/closure의 expected
revision을 CAS한 뒤 append-only `ProposalFindingBasisReceipt`를 함께 만든다. receipt는 org·
accepted finding closure ID/revision/digest·finding ID/revision·proposal ID/revision·claimed_at·
idempotency key를 결박하고 `(org_id, accepted_finding_closure_id)`에서 unique다. 같은 proposal·
같은 canonical basis set replay만 기존 receipt를 돌려주고, 하나라도 다른 proposal이 먼저 claim한
closure가 있으면 전체 proposal write는 0이다. AI principal이나 candidate generator는 basis를
claim하지 못한다.

일회성 저위험 답의 발송 안전은 기존 `ApprovalPolicy`가 맡는다. 답을 지식·템플릿·eval·판례로
재사용하려는 순간에는 반드시 `ArtifactRevision`으로 등록해 이 규칙을 적용한다.

## 결정 4 — Review Cycle과 실행 lease를 다른 revision으로 관리한다

한 필드로 사용자 편집, AI 실행 재시도, worker fencing을 표현하지 않는다.

- `cycle_revision` — 사람이 보는 Review Cycle 상태의 optimistic concurrency revision.
- `cycle_no`·`review_round` — 같은 revision의 역사적 재검토와 개선 계보.
- `review_run_id`·`run_attempt` — 같은 cycle에서 실행한 AI·사람 검토의 불변 실행 식별자와 시도.
- `lease_epoch`·full secret token — worker reclaim과 stale 결과 차단. full token은 발급 때 한
  번만 반환하고 저장소에는 hash만 둔 뒤 constant-time으로 비교한다. DB 시간이 lease 시각의
  기준이며, renew·reclaim은
  expected epoch·owner·expiry를 함께 대조하는 CAS다. 사용한 epoch는 tombstone으로 남겨 ABA를
  막는다.

release-governance 목적의 활성 cycle은 `(org_id, revision_id) WHERE active` partial unique
제약으로 revision마다 하나만 허용한다. 같은 canonical open은 기존 cycle을 돌려주고,
active cycle에 다른 policy나 purpose로 여는 요청은 conflict다. terminal 뒤에는 `cycle_no`,
purpose, policy version, review round가 다른 historical cycle을 열 수 있다. 모델 drift에 따른
재검토도 기존 cycle을 terminal로 닫은 뒤 새 cycle로 연다.

cycle을 열 때 policy snapshot과 `ReviewRequirement` 집합을 함께 고정하고 이후 수정하지 않는다.
각 requirement는 reviewer kind, `all | any | quorum`, required count, 독립성 규칙, rubric version,
deadline, risk class를 가진다. run 결과는 해당 requirement에 exact binding한다. 정책·법무 요건이
진행 중 바뀌면 기존 requirement를 고치지 않고 아래 공통 governance drift 계약으로 새 cycle을
만든다. 고위험 다중 독립 검토는 active cycle을 여럿 만드는 대신
한 cycle 안의 여러 requirement와 run으로 표현한다. 지속 감시와 reviewer 품질 표본은 이 cycle에
넣지 않고 Evaluation/Surveillance 경계로 분리한다.

저장 상태는 `ReviewOpen | AwaitingHumanDisposition |
BindingReady(action: ApproveRevision | RequestChanges | RejectRevision,
exact HumanDispositionReceipt) | BindingPending |
Bound(exact source BindingReceipt/outcome) | Superseded`의 닫힌 합이다. `unmet_requirements`,
`AwaitingAiReview`, `AwaitingHumanReview`는 불변 requirement, exact accepted `ReviewRun`, 정책상
유효한 `ReviewRequirementWaiver` receipt에서 계산하는 read projection으로만 두며 cycle 상태에
복제하지 않는다. run으로 충족한 `completed_requirements`와 waiver로 건너뛴
`waived_requirements`는 별 projection이다. waiver를 review completed로 표시하지 않는다.

사람 command와 source 결과 용어는 분리한다.
`BindingAction = ApproveRevision | RequestChanges | RejectRevision`,
`BindingOutcome = Approved | ChangesRequested | Rejected`이며 exact mapping은
`ApproveRevision→Approved`, `RequestChanges→ChangesRequested`, `RejectRevision→Rejected`다.
adapter receipt가 이 mapping과 다르면 `BindingReceiptMismatch`다.
사람 처분으로 넘어갈 때 `ReviewRequirementWaiver`를 다시 확인한다. `BindingPending`과 승격
직전에는 `ReviewRequirementWaiver | FindingRiskAcceptance | FindingPolicyException`의 issuer
권한·정책 버전·범위·만료를 모두 다시 확인한다. binding 전에 하나라도 만료되면 진행을 막고
기존 cycle을 `Superseded`로 닫은 뒤 새 요건으로 다시 연다. 이미 `Bound`가 된 뒤 승격 전에
만료되면 promotion write를 0으로 거부하고 `Bound` 이력은 보존한다. 같은 revision에 새
historical cycle과 현재 요건을 열어 다시 검토한다.

accepted review run 또는 waiver issuance 뒤 `unmet_requirements=0`이 되면 그 결과/receipt를
쓰는 **같은 transaction**에서만 `ReviewOpen → AwaitingHumanDisposition`으로 CAS한다. projection만
0으로 만들고 상태를 남겨 두지 않는다. waiver revocation·expiry가 아직 `ReviewOpen`일 때는
projection과 cycle revision만 갱신한다. 이미 Awaiting/BindingReady로 전진했다면 상태를 뒤로
돌리지 않고 nonterminal cycle을 `Superseded`로 닫아 새 requirement의 historical cycle을 연다.
`BindingPending` commit은 DecisionWindow authorization의 선형화점이다. 그 뒤 expiry·revocation은
in-flight source call과 경합해 cycle을 즉시 닫지 않고 `GovernanceDriftObserved`를 append해
promotion을 차단한다. exact source receipt로 `Bound`가 된 terminal transaction은 유효한 same
revision의 새 historical cycle을 함께 열 수 있다. binding failure로 `Superseded`가 되면 exact
`SourceReconciliationReceipt`가 같은 transaction에 있지 않는 한 새 cycle은 0이고 escalation한다.
`Bound`면 기존 terminal은 불변이고 새 historical cycle만 연다.
이 세 governance receipt의 `expires_at`은 binding/promotion 같은 결정을 내릴 수 있는
`DecisionWindow`다. 성공한 promotion의 역사적 사실을 시간이 지났다는 이유만으로 바꾸지는
않는다. serving 동안 계속 유효해야 하는 예외는 Phase 18 v0의 이 receipt로 표현할 수 없으며,
target-native/gateway enforcement와 preauthorized expiry operation을 가진 별 continuous-serving
policy가 생기기 전에는 허용하지 않는다. data access 자체를 낮추는 `DeclassificationReceipt`는
위 별도 fail-closed expiry 계약을 따른다.
`Bound`의 outcome은 기존 도메인의 exact receipt에서 파생하며, 별 `ReviewApproved`를 독립
진실로 저장하지 않는다. terminal cycle을 다시 열지 않는다. 사람이 candidate를 수정하면
parent를 가리키는 새 revision과 새 cycle을 만들며 같은 revision을 자가 승인하지 않는다.

모든 requirement와 finding closure가 유효할 때 사람의 revision disposition transaction은 세
action 모두 `AwaitingHumanDisposition → BindingReady(action, HumanDispositionReceipt)`로만
CAS한다. 이는 source 처분 결과가 아니라 권한 있는 사람의 binding 명령 의도다. adapter 호출
intent는 별 transaction에서 `BindingReady → BindingPending`으로 전이하고, 기존 source가 exact
action을 commit한 뒤 read-back한 receipt가 일치해야만 `Bound(outcome)`가 된다. 수정요청·거절도
Approve와 같은 경계를 타며 common ledger가 `StageReview` 등 source보다 먼저 결과를 확정하지
않는다. 장애가 나면 `BindingReady`/`BindingPending`을 scan해 전진 복구한다. 새 child revision은
source receipt에서 `Bound(outcome=ChangesRequested)`가 확인된 뒤에만 만들 수 있다.
`Bound | Superseded`만 terminal이며, approve/request-changes/reject 표시는 `Bound` source outcome의
read projection이다. source 없는 generic binding outcome은 Phase 18 범위에 두지 않는다.
promotion·package-ready에는 exact `Bound(outcome=Approved)`만 쓸 수 있고,
`Bound(outcome=ChangesRequested | Rejected)`는 자격이 0이다. 둘 모두 같은 revision·review purpose에
더 새 cycle이나 active cycle이 있으면 과거 Bound receipt를 재사용할 수 없다. current governance
epoch/policy에서 가장 최신의 non-superseded `Bound(Approved)`만 승격과 package-ready 자격 계산에
참여한다.

provenance, data boundary, policy/legal requirement, waiver/exception의 digest·validity가 바뀌면
`GovernanceDriftObserved(kind, old_digest, new_digest, observed_at, cycle_id,
binding_intent_id?)`를 append한다. BindingPending 전 nonterminal cycle은 expected cycle revision으로
`Superseded` 처리하고, 같은 revision이 여전히 유효할 때만 새 cycle·requirements·SLA를 같은
transaction에서 만든다. `BindingPending`은 source command의 선형화점이므로 state를 즉시 닫지
않는다. 새 model/source/serving/package와 promotion write는 0으로 막고 fenced source call을 exact
`Bound | BindingFailure/Superseded`까지 settle한다. `Bound` terminal이면 유효한 same revision의
새 historical cycle을 같은 transaction에서 열 수 있다. `BindingFailure/Superseded` terminal은
결정 5의 exact `SourceReconciliationReceipt`가 같은 transaction에 있을 때만 같은 revision을
다시 열며, 없으면 cycle write 0과 사람 escalation이다. data-boundary invalid처럼 same revision이
더는 유효하지 않으면 reconciliation receipt가 있어도 새 cycle 대신
보수적 boundary의 새 revision을 요구한다. 이미 `Bound`면 terminal은 불변이며 같은 유효성 규칙에
따라 새 historical cycle 또는 새 revision을 요구한다. late source mutation은 결정 5의 감사·
수동 reconciliation 경로를 따른다.

Review Cycle, finding, disposition, evaluation, promotion, audit 자체는 `ArtifactKind`가 아니다.
따라서 review-of-review 자동 재귀를 만들 수 없다. accepted finding은 원 산출물의 새
revision만 제안한다. 같은 content digest·purpose·policy·rubric·required model snapshot·
evidence boundary로 여는 중복 cycle은 거부한다. 정책·법무 요건, 모델·rubric drift, evidence
만료처럼 검토 근거가 달라진 경우에는 같은 digest도 새 historical cycle로 재검토할 수 있다.
한 자동 개선 사슬의 round
상한을 정책에 두고, 초과하면 사람 governance owner에게 escalation한다. 승격된 checkpoint
뒤에는 새 개선 cycle을 열 수 있으므로 지속 개선 자체를 막지는 않는다.

## 결정 5 — 기존 도메인이 binding SSOT를 계속 소유한다

새 문맥은 기존 승인·발행 상태를 복제하지 않는다.

- OKF 단계 전이는 `StageReview`가 계속 소유한다. adapter가 `set_disposition` 성공 뒤 exact
  receipt를 기록해야 cycle이 `Bound`가 된다.
- 답 발송은 `ApprovalItem`과 `ApprovalBoundary`가 계속 소유한다. Question Request는 기존
  `AwaitingApproval`에 머물며 별 `AwaitingReview`를 만들지 않는다.
- `BackupReview`와 `ReevalItem`은 각자의 사람 처분을 계속 소유한다. 안전한 adapter 계약을
  갖춘 뒤 그 exact receipt만 공통 원장에 투영한다.
- `CorrectionEvent`는 append-only 교정 원본이며 새 revision trigger다. `AnswerFeedback`은 abuse
  control과 사람 triage 전에는 약한 신호일 뿐이다. 둘 다 binding 처분이나 정답 SSOT가 아니다.
- terminal Question Request는 개선 때문에 되살리지 않는다. `request_id`와 `record_id`는
  계보 링크일 뿐이다. 사용자에게 고친 결과를 전달할 때는 기존 Correction·delivery outbox에
  새 사건을 남긴다.

adapter가 반환한 receipt가 원 도메인의 read-back과 다르면 cycle을 묶지 않고 fail-closed한다.
새 binding 포트는 같은 idempotency key·같은 canonical 명령만 같은 결과로 수렴시키고, 같은
키의 다른 명령은 conflict로 거부해야 한다. source commit 뒤 공통 receipt 기록이 실패하면
reconciler가 source를 다시 읽어 전진 복구한다.

외부 adapter 호출 전 `BindingPending`에 `binding_intent_id`·org·cycle·cycle revision·source
aggregate/ref·expected source revision·canonical action digest·adapter/schema version·policy
version·idempotency key·expected receipt digest를 durable하게 기록한다. reconciler는 이 의도와
source exact read-back이 모두 맞을 때만 `Bound`로 전진한다.

source가 명시적으로 영구 거절했거나, forward repair 뒤에도 expected receipt와 양립할 수 없는
exact read-back이 확정되면 `BindingPending`을 방치하지 않는다. original intent, adapter attempts,
최종 read-back digest, 실패 분류를 결박한 `BindingFailureReceipt`와 함께 cycle을
`Superseded(reason=binding_rejected|binding_receipt_mismatch|source_revision_drift)`로 한
transaction에서 닫고 사람
governance owner에게 escalation한다. 실패를 `Bound`로 위장하거나 같은 revision을 다시 열지
않는다. 원 source 상태를 사람이 정리하거나 새 revision을 만든 뒤에만 새 historical cycle을
열 수 있다.

같은 revision의 source 정리가 끝났다는 사실도 공통 원장이 선언하지 않는다. source owner의
권한 있는 사람이 수행한 cleanup action과 source의 exact stable read-back을 결박한
`SourceReconciliationReceipt`가 필요하다. 이 receipt는 org·failed binding intent/cycle·source
aggregate/ref·cleanup action receipt·source revision/state/digest·adapter/schema version,
issuer principal·role snapshot·policy version·reconciled_at·idempotency key를 고정한다. worker와
과거 call이 fence됐고 read-back이 새 cycle과 양립한다는 source adapter 증거가 없으면 발행할 수
없다. AI principal은 발행할 수 없고, common ledger가 source 상태를 대신 써서 만들 수도 없다.
revision과 data boundary가 여전히 유효할 때만 receipt와 새 historical cycle·requirements·SLA를
같은 transaction에서 만들 수 있다. receipt가 나중에 도착하면 그때 별 CAS transaction으로
cycle을 열며, `BindingFailure` terminal transaction이 먼저 끝났다는 이유만으로 자동 재개하지
않는다.

단, adapter call이 아직 in-flight이거나 late commit 가능성을 배제하지 못하면 permanent failure로
닫지 않고 `BindingPending`을 유지한다. worker lease를 fence하고 source의 expected-revision CAS·
semantic idempotency와 stable exact read-back으로 stale call의 추가 write가 0임을 확인한 뒤에만
`BindingFailureReceipt`를 쓸 수 있다. supersede 뒤 예상하지 못한 source mutation이 관측되면
`LateBindingMutationObserved`를 append하고 old cycle을 `Bound`로 되살리지 않으며, source owner에게
수동 reconciliation을 escalation한다. 이 fencing을 제공하지 못하는 source adapter는 binding
대상으로 enable하지 않는다.

현재 구현을 그대로 binding adapter로 간주하지 않는다. `StageReview`에는 actor·revision digest·
durable claim이 없고, `BackupReview`는 다른 처분의 재호출을 기존 결과로 수렴시킬 수 있다.
`Reeval`은 subject와 outcome 축이 어긋나도 항목을 닫을 수 있으며, `Correction` append와 reeval
적재는 원자적이지 않다. `CurationProvenance`도 인증된 사람 receipt가 아니라 문자열이다. 각
도메인이 exact read-back, semantic idempotency, durable recovery를 충족하기 전에는 common
ledger에 증거만 투영하고 binding 대상으로 열지 않는다.

## 결정 6 — 개선 대상을 닫고 자동 변경 금지 대상을 타입에서 뺀다

허용할 `ImprovementTarget`은 다음 닫힌 합으로 시작한다.

- `KnowledgeChange`
- `AnswerTemplateChange`
- `PromptChange`
- `ContextualPrecedentProposal`
- `RoutingRuleProposal`
- `EvalCaseAddition`

`RoutingRuleProposal`과 `PromptChange`는 change-control 패키지만 만들 수 있다. 이 문맥에는
중앙 규칙이나 운영 prompt를 직접 적용하는 포트를 두지 않는다. production code, Authority,
RBAC, ApprovalPolicy, secret, model weights는 표현할 타입 자체를 만들지 않는다.

두 package-only target은 평가와 사람 검토를 통과해도 `PromotionRequested`나
`ServingTargetState`를 만들지 않는다. 결과는 immutable `ChangeControlPackageReady` receipt이며,
별 소유 시스템의 승인·배포·rollback SSOT로 handoff할 뿐이다. 이 범위를 열려면 해당 시스템의
exact receipt와 복구 계약을 다루는 후속 ADR이 필요하다.

package-ready는 다음 exact `PackageAuthorizationSnapshot`을 가진다.

```text
proposal ID/revision + candidate/basis/dataset-reservation receipt IDs/digests
current governance epoch + latest Bound(Approved) cycle ID/revision/receipt
Evaluation ID/revision + PassedEvidence digest/expiry/scope
current policy/ACL/schema/boundary versions
package/downstream schema+expected downstream revision
issuer principal/role/policy + handoff_before + idempotency key
```

UoW는 해당 rows/revisions를 lock해 모두 conditional-check하고
`ChangeControlPackageReady` receipt, durable `PackageHandoffIntent(Pending)`, audit와 outbox를 한
transaction으로 쓴다. 이 commit이 package/handoff authorization DecisionWindow의 선형화점이다.
review가 current governance epoch/policy의 latest non-superseded exact `Bound(Approved)`가 아니거나
같은 revision·purpose에 더 새/active cycle이 있으면 package write는 0이다.

consumer는 downstream call 직전에 intent가 non-superseded인지, full-token-once/hash-at-rest claim
lease·fence, exact package/snapshot digest, expected downstream revision, `handoff_before`와 continuous
boundary를 다시 검증한다. downstream command도 expiry·expected revision·semantic idempotency를
조건부 집행해야 한다. drift가 claim/call 전이면 Pending을 Superseded로 CAS하고 call은 0이다. call이
in-flight이거나 late acceptance 가능성이 있으면 즉시 닫지 않고
`PackageGovernanceDriftObserved`를 append한 뒤 stable exact read-back으로
`PackageHandoffAccepted | PackageHandoffFailure`를 terminal 처리한다. supersede 뒤 예상하지 못한
acceptance는 `LatePackageMutationObserved`와 수동 reconciliation로 보내며 old intent를 Accepted로
되살리지 않는다. conditional expiry·idempotency·stable read-back fencing을 제공하지 못하는
adapter에는 handoff를 enable하지 않는다. exact acceptance receipt를 기록해도 이는 production
적용이나 `PromotionApplied`가 아니다. downstream deploy owner는 자체 최신 governance를 다시
검증해야 한다.

사람이 수용한 finding만 proposal의 근거가 될 수 있다. bad feedback, correction, 거절,
stale 표식은 review trigger이지 그 자체로 사실이나 승인된 finding이 아니다. 단일 feedback은
abuse control과 사람 triage를 거쳐 cycle 생성 신호로만 쓴다.

proposal 뒤에 생긴 candidate revision은 digest만 우연히 맞는 임의 산출물일 수 없게 한다.
append-only `ProposalCandidateReceipt`는 org·proposal ID, proposal revision, source accepted-finding
closure IDs·digest와 exact `ProposalFindingBasisReceipt` IDs·digest, base revision ID·
content/provenance/data-boundary digest, candidate revision ID·
content/provenance/data-boundary digest, candidate sequence, generator의 authenticated
`HumanPrincipal | ModelExecution`, generation policy·input·tool·retrieval digest, idempotency key를
결박하고, server-side로 exact opaque `EvaluationDatasetReservation` ID/digest도 연결한다. generator
응답에는 seal·dataset/split digest를 노출하지 않는다. candidate `ArtifactRevision`과 이 receipt는
같은 transaction에서 생성한다. 같은
proposal이 새 candidate를 만들면 sequence와 receipt가 새로 생기며 과거 candidate를 고치지
않는다. 이 transaction은 expected proposal revision을 CAS하고 `GovernedEvaluationUseReceipt` 부재를
검증하므로 evaluation freeze 뒤 candidate가 끼어들 수 없다. 같은 idempotency key·같은 candidate
payload는 기존 receipt를 돌려주고 다른 payload는 conflict다. evaluation queue와
promotion/package-ready 요청은 exact receipt ID와 모든 digest를
재검증하고, proposal에 연결되지 않은 revision은 거부한다.

## 결정 7 — 독립 평가 없이는 승격하지 않는다

evaluation dataset arm은 proposal generator나 runner가 고르지 않는다. authority policy registry가
target policy/scope에서 다음 닫힌 합을 결정한다.

```text
EvaluationDatasetReservation =
  HoldoutReservationReceipt
  | NoHoldoutRequiredReceipt(policy/scope digest, registry version,
                             authority, reason, canonical no-holdout digest)
```

`NoHoldoutRequiredReceipt`는 policy가 holdout 불필요를 명시하고 required axis가 label-dependent
`CandidateBaselineMetric`을 하나도 포함하지 않는 integrity/safety-only target에서만 가능하다.
독립 evaluation 자체를 면제하지 않으며 모든 required `IntegrityCheck | SafetyInvariant`는 그대로
실행한다. generator·runner가 이 arm을 만들거나 holdout-required policy를 낮출 수 없다.
두 arm은 `(org_id, proposal_id)`에서 합쳐 정확히 하나만 존재하고 same canonical replay만
허용한다.

holdout-required arm은 proposal 뒤에 dataset을 고르지 않는다. 독립 curator가 proposal 생성 전에 immutable
`HoldoutSealReceipt`를 만든다. receipt는 org·seal ID, dataset/version·item/split digest,
`sealed_at`, curator principal/role·independence snapshot, access-policy digest, target kind·
evaluation-policy/scope digest, label provenance/schema와 idempotency key를 결박한다. item·label·
split과 개별/aggregate 결과는 별 ACL 저장소에 두고 proposal generator·artifact reviewer가 읽지
못한다. `sealed_at`은 authoritative DB time이고 receipt는 proposal UoW보다 먼저 별 append-only
registry transaction에서 commit돼 ledger `commit_sequence`를 얻어야 한다. caller timestamp를
신뢰하거나 seal과 proposal을 같은 transaction에서 만드는 write는 0이다.

ImprovementProposal 생성 transaction은 policy가 선택한 exact dataset arm을 함께 고정한다.
holdout-required면 target policy/scope와 맞고 `sealed_at < proposal.created_at`인 seal 하나를
opaque ID로 예약해 `HoldoutReservationReceipt`를 만든다. receipt는 proposal
ID/revision, seal ID/digest, policy/scope, retry-lineage root·ancestor proposal IDs, reserved_at과
idempotency를 결박한다. `(org_id, proposal_id)`와 `(org_id, holdout_seal_receipt_id)`에서 unique이고,
same canonical replay만 허용한다. ancestor lineage가 사용한 dataset/split digest를 새 seal ID로
다시 포장해도 conflict/write 0이다. generator에게는 opaque reservation ID만 보이며 dataset
내용이나 digest를 노출하지 않는다. 새 proposal after Rejected는 새 human-accepted closure뿐 아니라
holdout-required arm이면 ancestor와 다른 independently sealed holdout reservation을,
no-holdout arm이면 current registry가 다시 발행한 exact `NoHoldoutRequiredReceipt`를 가져야 한다.
reservation UoW는 expected seal revision·unused 상태와
`seal.commit_sequence < proposal_creation_sequence`를 CAS한다.

논리 `Evaluation`은 `EvaluationQueued | EvaluationRunning(attempt_no) |
EvaluationRecorded(PassedEvidence | RejectedEvidence) | EvaluationInterrupted(attempt_no)`의
닫힌 합이고, 각 `EvaluationAttempt`와 interrupt receipt는 append-only다. 품질 gate를 통과하지
못한 `RejectedEvidence`와 실행 인프라가 끝까지 완료되지 않은 `EvaluationInterrupted`를
구분한다. 전자는 proposal을 `RejectedProposal`로 닫는다. 같은 proposal/dataset reservation에서
candidate만 바꿔 promotion gate를 반복 질의할 수 없다. holdout-required면 독립 curator가
proposal 생성 전에 따로 seal한 새 holdout을 결박한 새 proposal/candidate가 필요하다. 새
proposal은 eval 실패를 사람이 검토해
새로 accept한 finding/closure를 근거로 해야 하며, 이전 accepted finding의 exact closure를 두
proposal에 재사용하지 않는다.

후자는 별 `RetryEvaluation` command만 허용한다. command는 expected evaluation revision, exact
proposal/candidate/policy/scope/`EvaluationDatasetReservation` digest, previous attempt, 새 retry idempotency key를 결박하고,
`Interrupted(n) → Queued` CAS와 `attempt_no=n+1` 예약을 원자화한다. 같은 retry key·같은
payload는 같은 queued attempt를 돌려주고, 같은 key·다른 payload는 conflict다. 이전 attempt의
실행 idempotency key로 외부 runner를 다시 부르지 않으며 과거 interrupt receipt를 보존한다.
`EvaluationAttempt` lease도 결정 4의 full-token-once/hash-at-rest, DB time, constant-time compare,
owner·epoch·expiry CAS, reclaim tombstone 계약을 그대로 쓰며 review-run lease와 namespace를
분리한다. stale attempt 결과는 write 0이다.

logical Evaluation의 unique key는 `(org_id, proposal_candidate_receipt_id,
evaluation_policy_digest, target_scope_digest,
evaluation_dataset_reservation_digest)`다. 같은 canonical enqueue는 기존
aggregate를 돌려주고 다른 payload는 conflict다. policy·scope drift는 새 logical Evaluation을
같은 proposal 안에서 갈아끼울 수 없다. policy·scope·dataset reservation이 달라지면 기존 proposal을
`SupersededProposal`로 닫고 policy가 요구한 새 pre-proposal reservation과 새 policy를 결박한
proposal/candidate를 만들어야 한다. promotion은 current policy와 exact scope/dataset
reservation이 일치하는 aggregate만 사용할 수 있어 과거 PassedEvidence로 더 최신 정책이나
RejectedEvidence를 우회하지 못한다.

governed evaluation queue는 proposal 생성 때 이미 고정한 exact
`EvaluationDatasetReservation`·policy·scope를 다시 검증하고 `(org_id, proposal_id)` unique
`GovernedEvaluationUseReceipt(proposal_candidate_receipt_id,
evaluation_dataset_reservation_id, policy/scope/dataset-reservation digest)`를 expected proposal
revision CAS와 같은 transaction에서 만든다. 첫 enqueue는 candidate를 freeze할 뿐 dataset arm을
새로 고르지 않는다. 이후 같은 canonical enqueue만 기존 receipt를 replay하고 다른 candidate·
reservation·policy·scope enqueue는 conflict/write 0이다.
이 receipt 뒤에는 같은 proposal에 새
`ProposalCandidateReceipt`를 추가할 수 없다. `RejectedEvidence` terminal
transaction은 proposal도 `RejectedProposal`로 CAS한다. `EvaluationInterrupted`만 exact 같은
candidate·`EvaluationDatasetReservation`으로 위 `RetryEvaluation`을 허용한다. 개발·shadow
suite는 반복할 수 있지만
promotion evidence로 승격할 수 없다.

각 proposal은 권한 있는 policy registry가 target kind와 배포 scope로 선택한 immutable
`EvaluationRequirementPolicy` snapshot을 결박한다. proposal generator나 runner는 정책을
고르거나 축을 낮출 수 없다. 이 정책은
필수 평가 축, 각 축의 `CandidateBaselineMetric | IntegrityCheck | SafetyInvariant`, metric·
threshold, runner·rubric, holdout 필요 여부, target scope와 다음 baseline 합을 닫힌 값으로
정한다.

```text
BaselineReference =
  ServingRevisionBaseline(revision_id, serving_state_digest, runtime_snapshot)
  | ApprovedControlBaseline(artifact_ref, artifact_digest, curator_receipt)
  | NoPriorBaseline(policy_authorization, reason)
```

`NoPriorBaseline`은 current state가 `Uninitialized` 또는 prior가 없는 `Deactivated`이고 승인된
control도 없음을 registry가 증명한 target에서 policy가 명시적으로 허용할 때만 쓴다. promotion
직전에 이 부재를 다시 검증한다. 이 arm은 "baseline 대비 무회귀" 주장을 만들 수 없고, 더 엄격한 absolute
quality threshold·모든 safety invariant·shadow observation·제한 canary를 필수로 한다. 격리된
과거 revision을 안전한 baseline으로 자동 재사용하지 않는다. 필수 축에서 runner가
`None | Skipped`를 내면 실패다. `NotApplicable`은 runner가 임의로 고를 수 없고 해당 policy
snapshot이 그 축을 비적용으로 선언했을 때만 유효하다.

`EvaluationEvidence`는 proposal ID/revision, exact `ProposalCandidateReceipt` ID, candidate
revision/content/provenance/data-boundary digest, exact `BaselineReference`와 해당 arm의 digest
(`NoPriorBaseline`이면 absence authorization·reason), active base state, 적용 가능한 target·
environment/lane scope 또는 명시적 target-agnostic 표식, evaluation-policy version·digest,
target adapter·runtime snapshot, exact `EvaluationDatasetReservation` ID/digest/arm, runner·code SHA, model
snapshots, policy·ACL·schema version, 축별 결과와 `NotApplicable` 근거, metric·threshold, hard
invariant, 회귀 목록, 유효기한, 결과와 evidence digest를 보존한다. 승격 요청 target은 evidence
scope에 포함돼야 하며, target-agnostic evidence는 정책이 명시적으로 허용할 때만 재사용한다.
Holdout arm evidence에는 dataset/version·item/split·label seal을, NoHoldout arm에는 registry proof·
reason·canonical no-holdout digest를 저장한다.

- HoldoutReservation arm의 dataset version과 split은 proposal 생성 전에 독립 curator가 seal한다. proposal
  generator와 artifact content reviewer는 holdout item·label에 접근하지 못한다. evaluation
  runner는 같은 item 입력을 candidate·baseline runtime에 전달할 수 있지만 기대 label은 받지
  않는다. 두 출력이 고정된 뒤에만 격리된 metric/grader가 sealed label을 읽는다. 항목별 결과는
  승격 결정 전에는 generator·reviewer에게 공개하지 않는다. baseline arm이 있으면 candidate와
  baseline을 같은 runner·환경·provider snapshot으로 비교한다. `NoPriorBaseline`이면 같은 sealed
  입력에서 candidate의 absolute threshold만 재현 가능하게 측정한다.
  aggregate pass/fail을 포함한 governed-holdout 결과도 terminal
  promotion/package-ready/rejection 결정 전에는 generator에게 돌려주지 않는다.
- HoldoutReservation arm에서는 같은 proposal에서 만든 eval case를 그 proposal의 held-out 평가에 쓰지 않는다.
  `EvalCaseAddition`은 candidate·baseline 비교를 자기 자신으로 통과할 수 없다. 해당 target의
  정책은 authenticated label provenance, schema·중복·누출·split integrity를 필수로 검사하고,
  승격된 case는 이후에 독립 curator가 다시 seal한 미래 suite부터만 쓸 수 있다.
- classification·routing·answer-quality처럼 label-dependent 축이 target policy에 포함되면
  HoldoutReservation arm이 필수이고 모든 기대 라벨과 답
  기준에는 인증된 사람의 label provenance가 있어야 하고 답 기준은 비어 있을 수 없다. 필수
  기대값이 없거나 필수 축이 `None | Skipped`이면 승격 실패다. `ContextualPrecedentProposal`과
  `AnswerTemplateChange`처럼 영향 축이 다른 target은 각 policy가 필요한 축과 비적용 축을
  명시한다. 현재 기본 30문항은 routing 기대 필드에 인증된 label provenance가 없고 답 기준도
  0건이므로, migration 전까지 coherence 관측용일 뿐 어떤 target의 승격 증거도 아니다.
- 안전·권한·질문 종결률은 무회귀여야 한다. 답변 품질은 사람이 확정한 독립 holdout에서
  baseline 이상이어야 한다.
- grader 출력은 평가 증거이지 정답 라벨이 아니다. 골든 라벨은 사람 provenance 없이 만들거나
  수정할 수 없다.
- `EligibleForPromotion`은 영구 자격이 아니다. 승격 직전에 proposal→candidate exact link와
  candidate/base serving state·target operation epoch, review receipt, eval suite·target scope,
  policy·ACL·schema version, 유효기한을 다시 검증한다.
  review receipt는 exact `Bound(outcome=Approved)`이어야 하며 `ChangesRequested | Rejected`
  outcome은 항상 promotion write 0이다.
  같은 revision·review purpose의 더 새 cycle이나 active cycle이 있으면 과거 Bound receipt를
  재사용할 수 없다. current governance epoch/policy에서 가장 최신의 non-superseded
  `Bound(Approved)`만 자격 계산에 참여한다.
- 산출물 내용 검토와 production 승격 검토는 서로 다른 사람 증거다. 정책이 명시하지 않는 한
  한 번의 처분으로 두 결정을 합치지 않는다.
- ADR 0003의 "Precedent가 곧 eval label"은 폐기한다. 판례와 운영 결과는 큐레이션 후보가 될
  수 있지만, 사람이 확정한 라벨만 eval ground truth다. ADR 0041을 이 원칙의 기준으로 삼는다.

실 AI reviewer와 grader의 품질은 단위 테스트에 박지 않는다. FakeReviewer·FakeEvaluator로
구조를 결정론적으로 검증하고, 실제 정밀도·재현율·누락률은 게이트 밖에서 잰다.

## 결정 8 — 승격 이력과 serving target state를 분리한다

proposal의 쓰기 상태는 `OpenProposal | RejectedProposal | SupersededProposal`의 닫힌 합이다.
평가 실행과 승격 요청 상태를 proposal에 복제하지 않는다. `PromotedProposal`은 별
`PromotionApplied` receipt에서 계산하는 읽기 projection이며 독립적으로 쓸 수 없다. 승격
자격도 review·evaluation evidence에서 계산한 만료 가능한 projection이다. 승격된 역사적
사실은 롤백해도 해당 `PromotionApplied` receipt로 남는다.

제어 키는 `(org_id, artifact_id, target_id, environment_or_lane)`다. revision-only 포인터는
격리·비활성 상태를 표현할 수 없으므로 쓰지 않는다. target slot의 값은 다음 닫힌 합이다.

```text
ServingTargetState =
  Uninitialized
  | ServingRevision(revision_id, cause_request_id)
  | Quarantined(previous_serving_revision_id?, reason_code, cause_request_id)
  | Deactivated(previous_serving_revision_id?, reason_code, cause_request_id)
```

desired/in-process state와 외부 exact read-back projection을 같은 타입으로 가장하지 않는다.

```text
ObservedTargetState =
  RepresentableTargetState(
    state: ServingTargetState,
    raw_state_digest, external_version, external_generation, observed_at)
  | UnrecognizedExternalState(
    raw_state_digest, external_version, external_generation,
    adapter/schema version, observed_at, reason_code)
```

`ObservedArtifactState`는 이 합과 raw receipt를 담는 projection record다. external target exact
read-back이 serving SSOT라는 사실은 그대로이며, 해석할 수 없는 raw state를 임의의
ServingTargetState로 내리지 않는다. `UnrecognizedExternalState`는 즉시 DriftOpen이고 adopt·
compensate·normal promotion/rollback write는 0이다. exact raw external version/generation을 expected로
한 권한 있는 Quarantine/Deactivate만 허용한다. epoch>0 physical absence만 아래 규칙에 따라
Representable `Deactivated(reason=external_absent)`로 투영한다.

각 slot은 monotonic `target_operation_epoch`와 canonical state digest를 가진다. 모든 명령은
expected epoch와 expected state digest를 함께 대조한다. `previous_serving_revision_id`는 없을 수
있지만, reason과 원인이 된 request ID는 생략할 수 없다. exact 적용 영수증은 별 immutable
receipt로 남기며 previous/new **전체 state**와 target read-back을 결박한다.

물리 row가 없는 key는 해당 key·schema version으로 계산한 canonical `Uninitialized`, epoch 0,
state digest로만 읽는다. 첫 operation은 이 세 값을 expected로 가진 create-if-absent CAS이며,
unique key 충돌 loser는 write 0이다. 외부 target의 첫 적용도 "아직 존재하지 않음"을 나타내는
exact read-back token과 conditional create를 지원해야 한다. 이를 지원하지 않는 adapter는
initial activation target으로 열지 않는다. 따라서 첫 승격 32-way 경쟁도 후속 CAS와 같은
선형화 규칙을 쓴다.
한 번 생성한 slot row와 사용한 epoch는 삭제·재사용하지 않는다. serving 중단은 row 삭제가 아니라
`Deactivated` state와 더 큰 epoch로 표현해 ABA를 막는다.
`Uninitialized`는 control-plane slot row가 없고 epoch가 0인 최초 상태에만 유효하다. epoch가 한
번이라도 증가한 외부 target의 out-of-band absence는 raw absence token·external generation·
read-back digest를 drift receipt에 보존하되, domain actual state는
`Deactivated(previous_serving_revision_id?, reason_code=external_absent,
cause_request_id=drift_id)`로만 투영한다. epoch>0에서 `Uninitialized`를 adopt하거나 전이하려는
write는 0이다. 다시 활성화할 때는 current Deactivated state·더 큰 epoch와 exact absence token을
expected로 한 정상 Promotion conditional create를 쓴다. target이 삭제·재생성 사이를 구분하는
generation-fencing token을 제공하지 않으면 production target으로 enable하지 않는다.

- in-process target이 플랫폼 상태를 직접 읽으면 `ActiveArtifactState`가 serving SSOT다.
  `ActiveArtifactVersion`은 state가 `ServingRevision`일 때만 계산되는 read projection이다.
- Confluence·git·외부 배포처럼 target 자체가 상태를 소유하면 `DesiredArtifactState`가
  control-plane의 desired SSOT이고 외부 target exact read-back이 serving SSOT다.
  `ObservedArtifactState`는 그 read-back의 projection이지 두 번째 쓰기 진실이 아니다.

`ObservedArtifactState`는 캐시된 projection이므로 그것만으로 현재 serving 상태나 새 operation
자격을 판정하지 않는다. 플랫폼이 외부 target의 현재 serving/authorization 상태를 응답할 때,
모든 operation eligibility와 request 직전, terminal 기록 직전에는 current external version을
포함한 fresh exact read-back을 얻는다. background reconciler도 같은 검사를 반복한다. read-back이
마지막 Observed/Desired와 다르면 먼저 exact operation attempt receipt로 원인을 분류한다. current
non-superseded operation이면 정상 saga 진행이고, fenced/superseded attempt의 late commit이면 아래
`LateTargetMutationObserved` 경로다. current 또는 과거의 어떤 known attempt로도 설명되지 않을 때만,
expected desired·observed state/digest, actual target state·external version, 관측 시각과 원인을
결박한 `TargetDriftObserved`를 append하고 `ObservedArtifactState`를 actual로 갱신하며 slot의
`TargetDrift = DriftOpen`을 같은 transaction에서 연다. UI와 API는 stale desired를 serving으로
표시하지 않고 exact `RepresentableTargetState | UnrecognizedExternalState`와 drift를 함께
반환한다.

DriftOpen 동안 새 Promotion·Rollback과 desired 자동 재적용은 write 0이다. 기존 in-flight
operation이 원인이면 권한 있는 `RetrySameOperation | CompensateToObserved`만, 별 in-flight가
없으면 현재 policy·ACL을 다시 통과한 actual state에 대한 사람 `AdoptObserved`만 normal operation
재개 조건이 된다. 외부 `ServingRevision` 채택은 한 transaction의 direct desired write가 아니라
다음 durable saga다.

```text
TargetDriftAdoption =
  AdoptionPending(intent, reserved target epoch)
  | AdoptionApplied(TargetDriftAdopted receipt)
  | AdoptionDenied(reason)
  | AdoptionSuperseded(by higher target operation)
```

request transaction은 current `DriftOpen`, fresh exact actual revision/raw digest/external
version·generation, target slot/desired/epoch, issuer와 current policy·ACL·schema·boundary를 함께
conditional-check한다. actual이 exact known ArtifactRevision·proposal/candidate link, current
governance epoch의 latest Bound(Approved), target-scoped independent evaluation을 정상 Promotion과
똑같이 통과하거나, 같은 slot/state의 기존 exact `PromotionApplied | RollbackApplied` lineage와
current-known-good를 모두 통과해야 한다. 어느 arm도 과거 final enforcement receipt나 과거
gateway reference만 재사용하지 않는다. exact actual revision의 새 `BoundaryEnforcementPlan`,
`BoundaryDriftActionAuthorization`, optional declassification expiry schedule을 만들고 gateway
arm이면 새 `TargetGatewaySubject`의 `GatewayRouteReference(Pending)`도 같은 transaction에서 만든다.
그 뒤 slot의 next epoch와 non-superseded adoption intent를 예약하고 outbox를 쓰되, 아직 desired나
DriftResolved를 쓰지 않는다.

consumer는 외부 호출 직전에 pending intent·reserved epoch·current authorization과 target
external version을 다시 확인하고, 필요한 native/gateway continuous enforcement를 conditional
적용한다. terminal transaction은 adoption이 여전히 current non-superseded인지와 같은 exact target
revision/raw digest/external version·generation, gateway route/config generation·health 또는 native
grant/lease의 fresh exact read-back을 다시 대조한다. 일치할 때만 final
`BoundaryEnforcementReceipt`, gateway Pending→Active와 기존 보호 Active→Releasing, desired=exact
actual, reserved epoch 소비, `TargetDriftAdopted`, `DriftResolved`, audit/outbox를 함께 확정한다.
target이나 gateway가 중간에 바뀌면 이 terminal write는 전부 0이고 새 exact drift/attempt evidence를
남긴다. permanent failure·supersession의 Pending reference도 worker/attempt fence와 stable exact
미서빙·replacement/kill 보호가 확인되기 전에는 release하지 않는다. drift 중 권한 있는
Quarantine/Deactivate는 더 큰 epoch로 pending adoption을 Superseded할 수 있지만 normal operation은
그 adoption을 추월할 수 없다.

actual이 representable `Quarantined | Deactivated`인 safe-state adoption만 fresh actual state·raw
digest·external version/generation과 issuer 권한을 같은 transaction에서 검증해 desired를 exact
actual로 CAS하고 epoch를 증가시키며 `TargetDriftAdopted`로 drift를 닫을 수 있다. serving read를
여는 전이가 아니므로 gateway Active reference를 만들지 않는다. AdoptObserved는 검토·평가·경계
gate의 우회 포트가 아니다. unsafe·unknown·미표현 actual state는 adopt할 수 없고, epoch>0 absence는
위 Deactivated projection만 adopt할 수 있다. 그 밖에는 권한 있는 Quarantine/Deactivate만
허용한다. 그 뒤 원래 desired로 돌아가려면 별 정상 Promotion/Rollback을 새 expected state·epoch로
요청한다.

외부 target의 `PromotionRequested | RollbackRequested | CompensationRequested |
QuarantineRequested | DeactivateRequested | BoundaryLeaseRenewalRequested` transaction은 expected desired·expected
observed·expected
`target_operation_epoch`를 검증해 `DesiredArtifactState`와 epoch를 CAS하고 outbox를 함께 쓴다.
한 target slot에는 미종결 operation을 하나만 허용하며, observed와 desired가 다시 합치기 전에는
다음 operation을 받지 않는다. 따라서 늦게 도착한 외부 실행이 새 명령을 덮어쓰지 않고,
성공한 요청과 desired 상태도 갈라지지 않는다.

중앙의 immutable `TargetOperationPriorityPolicy`는 기본 순서를
`Deactivate > Quarantine > Compensation > Promotion = Rollback > BoundaryLeaseRenewal`로 닫는다.
일반 operation이
kill-switch보다 높아지도록 설정할 수 없다. strictly higher operation의 권한 있는 issuer가
current epoch와 exact pending operation을 지정할 때만 같은 transaction에서 lower operation을
해당 `*Superseded` arm으로 닫고 더 큰 epoch의 새 desired state를 쓴다. 따라서 Deactivate는
pending Quarantine·Compensation·Promotion·Rollback·BoundaryLeaseRenewal을, Quarantine은 pending Compensation·
Promotion·Rollback·BoundaryLeaseRenewal을 supersede할 수 있다. Promotion·Rollback은 pending
BoundaryLeaseRenewal을 supersede할 수 있고 Compensation도 renewal보다 높다. renewal은 어떤
operation도 supersede하지 못한다.
같은 우선순위끼리는 새 supersession이 아니라
idempotency/CAS 경쟁으로 닫고, Promotion·Rollback·Compensation은 kill-switch를 supersede할 수
없다.

`AdoptionPending`은 DriftOpen 안에서만 존재하는 resolution reservation이며 slot의 유일한
non-superseded in-flight로 센다. 일반 operation priority에 끼워 desired를 선택하지 않는다. 권한
있는 Quarantine/Deactivate만 exact pending adoption과 current reserved epoch를 지정해 더 큰 epoch로
이를 `AdoptionSuperseded`할 수 있고, Promotion·Rollback·Compensation·BoundaryLeaseRenewal과 다른
adoption의 경쟁 write는 0이다.

모든 consumer는 외부 호출 직전과 terminal 기록 직전에 epoch를 재검증한다. 이미 외부 호출이
시작돼 stale operation이 먼저 target을 바꿨다면 이를 Applied로 기록하지 않고
exact fenced/superseded attempt receipt를 결박한 `LateTargetMutationObserved`와 actual
`ObservedArtifactState`를 남긴다. 이는 unknown out-of-band drift가 아니므로 DriftOpen을 만들지
않는다. reconciler는 새 exact read-back을 기준으로, 이미 권한이 부여돼 아직 non-superseded인
highest operation 또는 compensation의 같은 idempotency key만 재시도한다. 새 desired를 고르거나
terminal을 추정하지 않는다. target adapter가 external version CAS를
지원하지 않으면 이 긴급 supersession 계약을 충족할 수 없으므로 production target으로 열지
않는다.

일반 operation이 영구 거절되거나 반복 read-back mismatch로 수렴하지 않으면 권한 있는 사람이
`RetrySameOperation | CompensateToObserved`를 선택한다. compensation은 current pending operation,
desired/observed 전체 state digest, target external version, epoch, issuer principal·role snapshot,
policy version, 사유를 결박한다. `CompensateToObserved`는 Promotion·Rollback의 영구 실행 실패에만
허용하는 별 `CompensationRequested → CompensationApplied | CompensationDenied |
CompensationSuperseded` aggregate다. 요청 transaction에서 original operation을 해당
`*Superseded(reason=compensation)` arm으로 원자 전이해
fence하고, desired state를 마지막 exact observed state로 CAS하며 epoch를 증가시킨다. 늦은 target
write가 생기면 새 read-back을 기준으로 desired state 복원을 재시도한다. exact 일치 뒤에만
`CompensationApplied` receipt로 compensation을 닫고 slot을 다시 연다. original operation을
Applied·Denied·Compensated로 중복 종결하지 않는다. 사람이 확인하지 않은 자동 desired
되돌리기나 AI principal의 compensation은 금지한다. 보상이 불가능하면 위 긴급
quarantine/deactivate만 허용하고 사건을 escalation한다.

compensation 대상도 gate 밖 상태를 채택할 수 없다. observed `ServingRevision`은 original
operation의 exact pre-state/read-back lineage와 같은 slot의 기존 `PromotionApplied |
RollbackApplied`에 연결되고 current policy·ACL·schema·boundary에서 known-good여야 한다.
`Quarantined | Deactivated`는 exact representable state와 issuer 권한을 검증한다. unknown revision,
미결박 external state, 만료·무효 boundary, epoch>0 `Uninitialized`면 CompensationApplied write는
0이고 Quarantine/Deactivate만 허용한다. out-of-band ServingRevision을 새로 채택하려면 위
AdoptObserved의 정상 Promotion-equivalent eligibility를 별도로 통과해야 한다.

`RevisionActivation = Promotion | Rollback`이다. 둘 다 target이 새 `ServingRevision`을 읽게 만드는
활성화이므로 exact target revision/current boundary에 대한 새 `BoundaryEnforcementPlan`,
`BoundaryDriftActionAuthorization`, optional declassification expiry schedule과 final
`BoundaryEnforcementReceipt`를 요구한다. 과거 revision의 예전 enforcement receipt를 그대로
재사용하지 않는다.

승격은 사람 principal·역할 snapshot과 다음 exact `PromotionAuthorizationSnapshot`을 검증한다.

```text
proposal ID/revision
ProposalCandidateReceipt ID/digest + candidate revision/content/provenance/boundary digest
current governance epoch + latest review cycle ID/revision + Bound(Approved) receipt
Evaluation ID/revision + PassedEvidence digest/expiry/target scope
policy/ACL/schema/current boundary version
baseline state/digest + target/environment + expected serving/desired state/epoch
promoter policy/version + apply_before + idempotency key
```

promotion request transaction은 이 aggregate row와 revision을 같은 lock order에서 읽어 expected
값을 모두 conditional-check한 뒤 request, target slot state/epoch와 outbox를 함께 CAS한다. 검증
read와 state write를 나누지 않는다. 이 commit이 promotion authorization DecisionWindow의
선형화점이다. in-process는 같은 transaction에서 `ServingRevision(candidate)`와 terminal receipt를
쓰고, 외부 target은 desired state를 쓴 뒤 consumer가 exact request snapshot을 실행한다. 같은 키·
같은 canonical payload는 같은 결과, 같은 키·다른 payload는 conflict다.

`apply_before`는 evidence·사람 권한·정책·boundary enforcement의 가장 이른 유효 종료와 adapter
safety margin 안이다. 외부 command에도 이를 넣어 target이 기한 뒤 commit을 조건부 거절하게
한다. 이를 지원하지 않는 adapter는 promotion target으로 열지 않는다. consumer는 호출 직전과
terminal 직전에 request가 current non-superseded operation인지, exact target epoch/external
version이 일치하는지 다시 확인한다. 호출 직전에는 아직 final receipt가 없으므로 exact
`BoundaryEnforcementPlan`과 current authorization·boundary validity를 검증하고, terminal 직전에는
실제 native/gateway enforcement를 fresh exact read-back한 뒤에만 final
`BoundaryEnforcementReceipt`를 만든다. 뒤늦은 non-boundary governance drift는 이미 선형화된
request를 몰래 고치지 않고 `PromotionGovernanceDriftObserved`로 남겨 이후 normal write를 막는다.
original request는 apply_before 안에서 exact 결과로 settle하고, 새 policy가
요구하면 별 권한의 higher-priority Quarantine/Deactivate를 발행한다. boundary drift는 위 per-read
deny와 preauthorized drift action이 즉시 우선한다. apply_before 초과나 supersession 뒤 stale
Applied write는 0이며, in-flight 여부를 fence한 뒤 exact observed state에 따라 compensation 또는
late-mutation reconciliation으로 닫는다. receipt는 target·environment·previous/new state·epoch과
authorization snapshot digest를 모두 결박한다.

Promotion과 Rollback은 각각 `PromotionRequested → PromotionApplied | PromotionDenied |
PromotionSuperseded`, `RollbackRequested → RollbackApplied | RollbackDenied |
RollbackSuperseded`로 관리하는 별 aggregate다. 외부 실행 실패는 정책 거절인 `Denied`로
위장하지 않고 request를 미종결로 둔 채 reconciliation attempt를 append한다. 외부 배포가
필요한 target은 desired/observed projection과 reconciler로 분리한다.

롤백은 과거 승격 기록을 수정하지 않는다. expected current version과 되돌릴 prior version을
가진 별 request를 먼저 남긴다. `RollbackAuthorizationSnapshot`은 exact current/prior full state·
target epoch/external version, prior의 PromotionApplied/RollbackApplied known-good lineage, current
policy·ACL·schema·boundary, exact prior revision의 새 activation `BoundaryEnforcementPlan`, issuer
principal/role, apply_before와
idempotency key를 결박한다. request transaction은 이 expected revision을 모두 conditional-check하고
해당 target의 `ServingRevision(current)` desired/active state·epoch CAS와 outbox를 원자화한다. 이
commit이 rollback authorization DecisionWindow의 선형화점이다. external consumer의
apply_before·boundary·epoch/version 재검증과 request 뒤 drift/supersession 처리는 promotion과 같은
계약을 쓴다. 성공한 exact read-back 뒤 receipt를 append하며, in-process는 active state와 receipt를
같은 transaction에 쓴다. receipt와 serving read-back으로 현재 상태를 투영한다.
과거 revision이라는 이유만으로
안전하다고 보지 않는다. 롤백 대상도 현재 ACL·schema·금지 콘텐츠 정책을 통과한 known-good
version이어야 한다.

현재 정책을 통과하는 prior version이 없으면 임의의 과거 버전으로 돌아가지 않는다. 별 권한을
가진 사람이 kill-switch를 실행한다. 상태는
`QuarantineRequested → QuarantineApplied | QuarantineDenied | QuarantineSuperseded`와
`DeactivateRequested → DeactivateApplied | DeactivateDenied`의 별
aggregate다. 명령과 receipt는
target·environment, expected current serving/desired state·epoch, issuer principal·role snapshot,
policy version, reason, idempotency key를 결박한다. 적용 결과는 각각 `Quarantined`와
`Deactivated` state다. 별 boolean kill-switch나 revision 포인터가 이 상태보다 우선하는 이중
SSOT는 두지 않는다. Promotion·Rollback·Quarantine·Deactivate·Compensation·
BoundaryLeaseRenewal은 같은 target/environment
slot과 epoch를 공유한다. 여섯 operation의 같은 expected epoch 혼합 32-way 경합은 한 request만
이기고, slot에는
non-superseded in-flight operation이 최대 하나다. 이후 권한 있는 긴급 supersession은 더 큰
epoch의 새 request이므로 accepted history가 둘 이상일 수 있지만, 이전 request는 반드시
`*Superseded`이고 stale Applied write는 0이다. 최종 serving state는 하나다. 적용된 kill-switch는
exact serving state read-back receipt로 증명한다. 외부 target이 해당 state를
원자적으로 적용하고 읽어낼 수 없으면 그 target에는 kill-switch를 enable하지 않는다. AI
principal은 이 요청을 실행할 수 없다.

## 결정 9 — 외부 실행은 durable saga로 묶는다

다음 묶음을 각각 한 DB transaction으로 commit한다.

1. revision + verified data-boundary snapshot + optional authorized `DeclassificationReceipt` +
   optional exact `ProposalCandidateReceipt`/expected proposal revision CAS + cycle + immutable
   requirement set + assignee/SLA + audit + outbox
2. provenance resolution/revocation receipt + expected effective-provenance digest CAS +
   BindingPending 전 nonterminal old cycle이면 `Superseded` CAS/새 historical cycle,
   BindingPending이면 drift receipt만(Bound면 불변/새 historical cycle) +
   requirement/assignee/SLA + audit + outbox
3. review run attempt/lease + requirement claim + runner dispatch outbox
4. fenced review run + signed finding batch + requirement completion/cycle CAS + unmet=0이면
   `ReviewOpen → AwaitingHumanDisposition` CAS + outbox
5. 사람 finding disposition + accepted finding의 exact closure
   (`ImprovementProposal | FindingRiskAcceptance | FindingPolicyException`) + ImprovementProposal이면
   모든 basis closure의 expected revision CAS/unique `ProposalFindingBasisReceipt` + proposal 전
   policy-selected `EvaluationDatasetReservation`(holdout arm이면 sealed·independent·ACL-safe
   expected seal CAS/unique opaque reservation/retry-lineage digest nonreuse, no-holdout arm이면
   registry proof·canonical digest) + revision 전체
   disposition command + cycle `BindingReady(action, HumanDispositionReceipt)` CAS + audit + outbox
6. `ReviewRequirementWaiver` 발급/철회 receipt + expected cycle revision CAS + 발급 뒤 unmet=0이면
   `ReviewOpen → AwaitingHumanDisposition`, 진행 뒤 철회면 nonterminal `Superseded`/새 cycle
   (`BindingPending`이면 drift receipt만, Bound는 불변+새 cycle) + audit + outbox
7. binding intent + exact `SourceBoundaryEnforcementPlan` +
   `BoundaryDriftActionAuthorization` + `BindingReady → BindingPending` cycle CAS + outbox
   + gateway arm이면 exact `SourceGatewaySubject`의 `GatewayRouteReference(Pending)`
8. fenced source stable exact read-back과 일치하는 action/outcome/
   `SourceBoundaryEnforcementReceipt` BindingReceipt +
   `BindingPending → Bound(outcome)` cycle CAS + gateway arm이면 source가 serving일 때
   Pending→Active와 old protected Active→Releasing, verified non-serving일 때 Pending→Releasing 또는
   native arm이면 exact revision/content·binding generation의 external NativeServingBinding
   Pending→Active와 per-read self-deny mode stable read-back 또는
   `BindingFailureReceipt` + `BindingPending → Superseded` cycle CAS; pending governance drift가
   있으면 Bound는 유효한 same revision의 새 historical cycle, BindingFailure는 exact
   `SourceReconciliationReceipt`가 같은 transaction에 있을 때만 새 cycle, 그 외에는 cycle write
   0·escalation; invalid data boundary면 언제나 새 revision 요구 event + audit + outbox
9. evaluation queue + proposal의 exact frozen requirement-policy/target-scope/
   `EvaluationDatasetReservation` 재검증 + unique `(org_id, proposal_id)`
   `GovernedEvaluationUseReceipt` + expected proposal revision CAS/candidate freeze + outbox
10. evaluation attempt/lease + `Queued → Running` CAS + runner dispatch outbox
11. fenced evaluation attempt + `Running → Recorded | Interrupted` CAS +
   evidence/interrupt receipt + Rejected면 proposal `RejectedProposal` CAS + outbox
12. 외부 target operation request, 긴급 supersession, authorized compensation 또는 lease-renewal
   intent + Promotion이면 exact `PromotionAuthorizationSnapshot`의 proposal/candidate/governance/
   review/eval/policy/ACL/schema/boundary/baseline/target aggregate revision conditional-check +
   Rollback이면 exact `RollbackAuthorizationSnapshot`의 current/prior state, prior applied lineage,
   policy/ACL/schema/boundary/target aggregate revision conditional-check + compensation이면
   prior-applied/current-known-good 또는 safe-state eligibility + renewal이면 exact renewal
   authorization/current-proposed lease/source boundary + `DesiredArtifactState`와 desired boundary
   metadata/epoch CAS + RevisionActivation이면 verified continuous `BoundaryEnforcementPlan`/
   `BoundaryDriftActionAuthorization`와 optional preauthorized expiry schedule + gateway arm이면
   exact `GatewayRouteReference(Pending)` + outbox
13. apply_before·non-superseded request·continuous boundary·target external version 재검증과 외부
   exact read-back 뒤 `ObservedArtifactState(ObservedTargetState)` projection + 해당 target operation의
   `*Applied` 또는 별 compensation의 `CompensationApplied` terminal CAS + RevisionActivation이면 native
   revision/content·activation generation의 NativeServingBinding Pending→Active와 TTL/ACL/continuous
   mode 또는 gateway current route/config/health+serving identity attestation을 fresh read-back한
   exact `BoundaryEnforcementReceipt` + gateway arm이면 new Pending→Active/old protected
   Active→Releasing + exact operation receipt + outbox
14. in-process operation request + Promotion/Rollback이면 12번과 같은 exact authorization snapshot
   aggregate revision conditional-check + operation-specific eligibility + `ActiveArtifactState`/epoch
   CAS + RevisionActivation이면 exact new revision의 optional preauthorized expiry schedule/
   `InProcessBoundaryEnforcementReceipt`/`BoundaryDriftActionAuthorization` + exact terminal receipt +
   outbox
15. package-only target의 exact `PackageAuthorizationSnapshot` proposal/candidate/basis/
   dataset-reservation/governance/review/evaluation/policy/ACL/schema/boundary/downstream revisions
   lock·conditional-check + `ChangeControlPackageReady` receipt + `PackageHandoffIntent(Pending)` +
   audit + handoff outbox
16. claim lease/fence·handoff_before·continuous boundary·expected downstream revision 재검증 +
   downstream stable exact acceptance/failure read-back + immutable `PackageHandoffReceipt |
   PackageHandoffFailureReceipt` + handoff terminal CAS + drift/late-mutation audit + outbox

`RetryEvaluation`은 별 DB transaction에서 expected evaluation revision과 previous attempt를
대조하고 exact proposal/candidate/policy/scope/dataset-reservation을 다시 확인해
`Interrupted → Queued`, 새 attempt number·retry idempotency reservation, audit·outbox를
함께 commit한다. 이후 10→11번을 다시 타며 이전 attempt나 interrupt receipt를 수정하지 않는다.

`HoldoutSealReceipt`는 proposal UoW와 분리된 선행 transaction에서 authoritative DB time,
append-only registry sequence, curator independence·ACL·policy/scope와 dataset/split digest를
commit한다. proposal UoW는 expected seal revision/unused CAS와 strict prior sequence를 검사하며,
same-transaction seal+reservation이나 caller-supplied backdate는 write 0이다.

`BoundaryLeaseRenewalRequested`는 12번에서 current policy revision·classification eligibility·
allowed max lag·current source boundary·issuer authorization·declassification expiry를 모두
conditional-check하고 slot epoch를 예약한다. 13번 exact lease/boundary/external-version read-back
뒤에만 Applied다. mismatch·policy drift·expiry는 renewal write 0과 preauthorized boundary drift
action으로 간다.

`GatewayRouteReference` cleanup은 subject별 worker/attempt fence, target 또는 source의 fresh stable
read-back, exact protected revision 미서빙과 replacement final enforcement 또는 fail-closed
kill/deny protection을 한 transaction에서 재검증해 Pending/Active→Releasing 또는
Releasing→Archived를 CAS한다. target A→B terminal의 old-reference Releasing은 13번에, source
replacement/Bound terminal은 8번에 포함한다. route/config fence는 target·source subject의 모든
non-Archived reference를 함께 본다. late mutation 가능성이 남으면 cleanup write는 0이다.

외부 target drift 관측은 fresh exact read-back과 expected Observed/Desired/external version을
대조해 `TargetDriftObserved` + `ObservedArtifactState` actual projection CAS + `DriftOpen` +
audit·escalation outbox를 한 transaction으로 확정한다. 사람 `AdoptObserved`가 current external
ServingRevision을 채택하면 첫 transaction에서 exact actual·Promotion-equivalent 또는 prior-applied/
current-known-good eligibility·새 boundary plan/drift authorization/expiry를 검증하고 next slot epoch와
`AdoptionPending`을 예약한다. gateway arm은 exact `TargetGatewaySubject` Pending을 같은 transaction에
만든다. worker가 DB lock 밖에서 enforcement를 조건부 적용·read-back한 뒤 두 번째 transaction이
same external version/generation과 final enforcement를 재검증해 receipt, Pending→Active/old
Active→Releasing, desired/epoch, `TargetDriftAdopted`/`DriftResolved`를 함께 확정한다. safe-state
adoption만 representable-state·issuer 권한 검증과 desired/epoch CAS를 한 transaction에서 닫는다.
기존 in-flight operation의 retry/compensation과 더 높은 kill-switch는 12→13번을 재사용하되 fresh
external version을 expected로 삼는다. 어떤 reconciler도 drift를 발견한 뒤 desired를 자동으로
재적용하지 않는다.

`SourceReconciliationReceipt`가 BindingFailure terminal 뒤 도착하면 exact failed intent/cycle,
source cleanup receipt·stable read-back, current revision/data-boundary validity와 expected active
cycle absence를 대조해 receipt + 새 historical cycle·requirements·assignee/SLA + audit·outbox를
별 transaction으로 확정한다. duplicate canonical receipt는 replay하고 source read-back이나
issuer/policy가 다르면 write 0이다.

`DeclassificationReceipt` 만료·무효화 또는 authoritative source boundary drift는 immutable
`BoundaryDriftObserved`/`DataBoundaryInvalidationReceipt`, affected slot·source refs, audit,
escalation outbox를 한 transaction으로 commit한다.
BindingPending 전 nonterminal cycle만 expected cycle revision을 대조해 `Superseded`로 CAS한다.
`BindingPending`이면 state를 바꾸지 않고 invalidation/drift receipt와 promotion write 0을 먼저
확정한 뒤 8번에서 exact source Bound/Failure까지 settle한다. 이미 `Bound`라면 terminal cycle은
고치지 않는다. declassification expiry면 invalidation receipt에
affected target slot은 `ScheduledBoundaryExpiryAction`을, source ref는
`SourceBoundaryDriftActionAuthorization(predicate=declassification_expiry)` 집행을 연결한다.
일반 boundary drift면 affected 경계에 따라 exact
`TargetBoundaryDriftActionAuthorization | SourceBoundaryDriftActionAuthorization`을 연결한다.
어떤 경우에도 같은 낮은-
boundary revision의 새 cycle은 만들지 않는다. data owner가 보수적 boundary의 새 revision을
등록하는 1번 UoW로만 다시 시작한다.

target arm의 유효 `BoundaryDriftActionAuthorization`은 12→13 또는 14번의 정상 shared slot saga를,
source arm은 별 drift-action intent/outbox와 terminal receipt UoW에서 결정 5·7→8번과 같은
source-owned expected-revision/idempotent/fenced adapter·stable read-back 경계를 재사용한다. old
ReviewCycle terminal은 고치지 않는다. handler는 authorization에 없는 action을 선택하지 않으며
common ledger가 source state를 직접 쓰지 않는다.

`ScheduledBoundaryExpiryAction` 집행은 원 사람 authorization과 exact expected state를 다시
검증하고 정상 `QuarantineRequested | DeactivateRequested`를 만든다. 외부 target이면 12→13번,
in-process면 14번의 같은 shared slot·epoch·CAS·receipt 규칙을 재사용한다. 새로운 AI 처분,
별 direct-CAS 포트, 우회 adapter를 만들지 않는다. source expiry에는 이 target-only 타입을 쓰지
않고 위 source drift-action intent/receipt UoW를 쓴다.

모델 호출, eval runner, 기존 도메인의 binding adapter, 외부 target adapter, package handoff
adapter는 DB lock 밖에서
호출한다. 3·7·10·12·15번의 durable attempt/intent/request와 transactional outbox를 먼저 만들고
consumer inbox dedup으로 외부 실행을 전달한다. exact receipt read-back 뒤에만 4·8·11·13번으로
terminal을 묶으며 package handoff는 16번으로 닫는다. 장애 뒤에는 pending scan, lease reclaim,
미전달 outbox, 같은 idempotency key로 복구한다.

15번은 package가 준비됐다는 로컬 사실과 handoff 의도를 함께 확정한다. downstream 호출 뒤
fenced stable read-back으로 exact acceptance 또는 permanent failure를 확인한 경우에만 16번으로
terminal을 닫는다. conditional expiry·semantic idempotency·expected revision과 stable read-back
API가 없으면 adapter를 enable하지 않고, pending 건은 사람에게 escalation할 뿐 전달 완료나 운영
적용을 추정하지 않는다.

외부 target에는 14번의 DB 원자성을 주장하지 않는다. 12번 desired state+request+outbox commit
뒤 DB lock 밖에서 expected observed state·external version으로 target CAS를 실행한다. exact
target read-back 뒤에만 13번을 commit한다. target이 거부하거나 read-back이 다르면 desired와
observed의 불일치를 숨기지 않고 operation을 pending/failed-reconciliation 상태에 둔 채 다음
새 일반 operation을 막는다. 같은 operation의 retry, 권한 있는 compensation, 더 높은 우선순위의
kill-switch만 각각 위 retry·compensation·supersession 규칙으로 전진할 수 있다. compensation은
12번으로 desired를 observed에 수렴시키고 13번 exact receipt 뒤 slot을 다시 연다. in-process
target만 14번 한 transaction에 state CAS와 receipt를 함께 넣을 수 있다.

잠금 순서는 `ArtifactRevision → ProvenanceResolution → ReviewCycle → ReviewRun →
Finding/Disposition → HoldoutSeal/Reservation → Proposal → ProposalFindingBasis →
BindingIntent/EvaluationAttempt → ServingTargetSlot →
TargetDrift → TargetDriftAdoption → TargetOperation → GatewayRouteReference → ScheduledBoundaryExpiryAction →
PackageHandoff → Outbox`다. expiry action은
항상 ServingTargetSlot/TargetOperation 아래에서 잠그며 별 역순 경로를 만들지 않는다.
Request·Registry·Authority lock을 잡은 채 이 문맥에
진입하지 않는다. P17.9의 durable DB·lease·outbox가 없으면 production activation을 열지 않는다.

## 결정 10 — 데이터와 prompt injection을 신뢰 경계에서 막는다

- 모든 aggregate key와 unique index에 `org_id`를 포함한다. `content_ref` 조회와 review 결과
  열람 시점마다 현재 RBAC·membership·purpose·classification version을 다시 확인한다.
- finding과 proposal은 원 revision 이상의 민감도와 ACL을 상속한다.
- AI 검토 입력을 최소화하고 PII redaction snapshot과 digest를 남긴다. 외부 모델 전송 전
  DLP, tenant opt-out, 처리 지역, 보존 정책을 검사한다. secret/restricted 자료는 승인된 내부
  모델이 아니면 AI 검토를 시작하지 않는다.
- 산출물은 untrusted data로 감싸고 system rubric을 고정한다. tool/network는 기본 0이며,
  evidence ref와 target은 allowlist로 제한한다.
- 산출물 안의 지시는 reviewer role, policy, target, promotion을 바꾸지 못한다.
- finding의 evidence span은 immutable content digest와 offset을 검증한다. finding의 Markdown·
  HTML도 비신뢰 출력으로 취급해 UI에서 escape한다.
- provider, model, deployment, prompt, rubric, policy version을 영수증에 고정한다. model drift는
  evidence 만료와 재검토 trigger일 뿐 자동 승격·롤백이 아니다.
- reviewer model alias는 실행 시 immutable snapshot으로 해석한다. 새 snapshot은 calibration
  suite, shadow 비교, 사람 enablement를 거쳐야 하며 과거 evidence를 덮어쓰지 않는다.
- lease·SLA·waiver·exception·evidence·declassification 만료는 authoritative DB time으로
  판정한다. target-native expiry는 provider timestamp와 허용 clock-skew budget을 receipt에
  결박한다. 신뢰할 clock이나 fail-closed safety margin을 제공하지 못하는 target에는 expiring
  artifact를 RevisionActivation하거나 external ServingRevision으로 adoption하지 않는다.
- revision 본문, finding evidence, 모델 receipt, 사람 처분, promotion receipt는 각 데이터의
  보존·삭제·legal hold 정책을 따른다.
- 조직이 다른 산출물·finding·평가·승격 정보는 같은 NotFoundOrDenied 결과로 숨긴다.

## 오류 계약

구현은 적어도 다음 의미 오류를 닫힌 결과나 명시적 예외로 구분한다.

- 조회·권한: `ReviewNotFoundOrDenied`, `PromotionUnauthorized`, `RollbackUnauthorized`,
  `QuarantineUnauthorized`, `DeactivateUnauthorized`, `CompensationUnauthorized`,
  `DeclassificationUnauthorized`, `BoundaryDriftActionUnauthorized`,
  `SourceReconciliationUnauthorized`
- 무결성·경합: `ProvenanceIntegrityError`, `ProvenanceResolutionConflict`,
  `DataBoundaryDowngrade`, `DataBoundaryInvalidated`, `CandidateLinkMismatch`,
  `ReviewIntegrityError`, `ActiveReviewConflict`, `ReviewCycleRevisionConflict`,
  `GovernanceDriftConflict`, `GovernedEvaluationConflict`, `HoldoutSealInvalid`,
  `HoldoutReservationConflict`, `ProposalFindingBasisConflict`,
  `SourceReconciliationConflict`,
  `ServingStateConflict`, `TargetOperationInFlight`, `TargetPriorityViolation`,
  `TargetReadbackMismatch`, `UnrecognizedExternalStateConflict`, `TargetDriftConflict`,
  `BoundaryEnforcementMismatch`, `GatewayRouteReferenceConflict`,
  `BoundaryLeaseRenewalConflict`, `PromotionAuthorizationConflict`,
  `RollbackAuthorizationConflict`, `PackageAuthorizationConflict`, `RollbackConflict`
- 실행·멱등: `ReviewRunConflict`, `ReviewLeaseLost`, `ReviewIdempotencyConflict`
- 검토 정책: `OriginPolicyViolation`, `ReviewerIndependenceViolation`, `FindingBatchMismatch`,
  `FindingDispositionIncomplete`, `BindingReceiptMismatch`
- 개선·평가: `ImprovementTargetForbidden`, `EvaluationMissing`, `EvaluationContaminated`,
  `EvaluationScopeMismatch`, `EvaluationPolicyMismatch`, `EvaluationRetryConflict`,
  `EvaluationExpired`, `CompensationConflict`, `PackageHandoffConflict`

tenant 존재를 추측하지 못하도록 외부 표면에서는 미존재와 접근 거부를 같은 결과로 투영한다.

## 단계

- **P18 S0 — SSOT·ADR.** 이 ADR, PRD/TRD/TASK/CONTEXT, ADR 0003·0041 정합화.
- **P18 S1 — revision/provenance/review ledger.** durable Store, cycle, claim, assignment, audit.
- **P18 S2 — 사람 작성 OKF·Correction → AI 자문 → 독립 사람 처분.**
- **P18 S3 — AI 작성 OKF → 기존 StageReview binding.**
- **P18 S4 — KnowledgeChange proposal → held-out eval → 사람 승격 → git/Knowledge version 롤백.**
- **P18 S5 — prompt/template/eval case, P17.10 뒤 contextual precedent proposal.**
- **P18 S6 — 운영 UI, ACL, 알림, 감사 내보내기, model drift, rollback drill, 파일럿.**

진입 게이트는 세 단계다.

1. **P17.8·P17.9 전** — S1~S3의 순수 도메인과 adapter shape를 Fake/Stub·synthetic data로만
   검증한다. 실제 source binding write는 0이다.
2. **P17.8·P17.9 뒤, P17.11·P17.12·P17.13 전** — 실제 조직 데이터의 durable review와 shadow
   eval을 허용한다. `ActiveArtifactState`·`DesiredArtifactState`·외부 target write는 0이다.
3. **다섯 게이트 완료 뒤** — 제한 canary, 별 사람 승격, target별 serving state CAS를 순서대로
   연다. 어느 단계에서도 AI principal의 binding·승격·롤백·kill-switch write는 0이다.

## 첫 파일럿

처음부터 범용 산출물 플랫폼을 만들지 않는다. Confluence/OKF 한 스페이스에서 다음 왕복을
shadow·assisted mode로 검증한다.

1. 사람이 수정한 지식 revision을 AI가 자문 검토한다.
2. 사람이 finding을 accept·reject·defer한다.
3. accepted finding으로 AI가 candidate revision을 만든다.
4. 기여자와 다른 사람이 candidate를 binding 검토한다.
5. 오염되지 않은 holdout을 통과하면 별 권한자가 한정 canary로 승격한다.
6. rollback drill로 이전 known-good version 복구와 감사 계보를 확인한다.

P17.11 eval과 P17.12 shadow 통합 리허설에서 실제 질문 100~200건, 지식 revision 50~100건을
관측 후보 규모로 삼되,
최종 표본과 임계는 파일럿 Owner와 사전에 확정한다. AI finding의 사람 확인 precision, 심은 결함
recall, blind holdout 품질, 수동 검토 대비 cycle time·사람 투입시간, candidate 수정·거절률,
승격 뒤 correction·bad feedback·rollback률을 함께 본다. ACL·PII 누출, 무승인 승격, stale lease
결과 수용은 0건이어야 한다. acceptance rate 하나만으로 효용을 주장하지 않는다.
검토 시간·수정 거리·독립 재감사 표본은 rubber-stamp 탐지 신호로만 쓰고 개인 고과로 전환하지
않는다.

## 수용 기준

- provenance와 보존 가능한 version-addressed `content_ref`가 없는 revision은 등록되지 않는다.
- 사람 기원 revision은 AI review batch 또는 정책이 허용한 유효 `ReviewRequirementWaiver` 없이는
  사람 처분 단계로 가지 않는다. waiver 경로를 AI 검토 완료로 표시하지 않는다.
- AI·mixed revision은 인증된 사람 binding receipt 없이는 발행·승격 write가 0이다.
- AI principal은 사람 finding/revision disposition, waiver/exception·provenance resolution·
  declassification, promotion·rollback·quarantine·deactivate·compensation·package-ready를
  실행할 수 없다.
- requirement와 policy snapshot은 cycle 안에서 바뀌지 않는다. 정책 변경은 기존 cycle을
  `Superseded`로 닫고 같은 transaction에서 새 cycle을 만든다.
- finding·feedback·correction·reeval은 활성 상태를 직접 바꾸지 않는다.
- defer된 finding은 담당자·SLA 없이 방치되지 않고, accepted finding은 proposal·
  `FindingRiskAcceptance`·`FindingPolicyException` 중 하나로 닫힌다.
- 동일 active cycle 경쟁은 한 winner이고 stale lease 결과는 write 0이다.
- 기존 binding receipt와 공통 원장이 다르면 fail-closed한다.
- `BindingFailure`로 Superseded된 same revision은 source owner의 cleanup action과 stable exact
  read-back을 결박한 유효 `SourceReconciliationReceipt` 없이는 새 cycle을 열지 않는다.
- accepted finding이 아니면 proposal을 만들지 않는다.
- accepted finding closure는 unique `ProposalFindingBasisReceipt`로 한 proposal에만 소비한다.
  같은 canonical replay가 아닌 32-way 중복 proposal 생성은 한 winner이고 나머지는 write 0이다.
- candidate `ArtifactRevision` 생성과 같은 transaction에서 exact `ProposalCandidateReceipt`로
  proposal/base/candidate가 연결되지 않으면 그 revision을 평가·승격·package handoff에 쓸 수 없다.
- forbidden target은 표현하거나 적용할 수 없다.
- holdout-required arm의 unsealed holdout, label provenance 누락, held-out 오염과 모든 arm의
  평가 누락·skip·hard invariant 회귀는 승격을 막는다.
- governed holdout은 독립 curator가 authoritative DB time·ledger sequence로 proposal보다 앞선
  별 transaction에서 seal하고 proposal 생성 transaction이 opaque reservation을 한 번만 claim한다.
  같은 transaction seal, backdated caller time, ancestor retry lineage와 같은 seal·dataset/split
  digest 재사용, proposal 뒤 holdout 선택은 write 0이다. NoHoldout arm은 current authority policy가
  명시한 integrity/safety-only target에서만 가능하다.
- 같은 proposal의 governed-evaluation enqueue 32-way 경쟁은 한
  candidate/policy/scope/dataset-reservation freeze만 남고, 다른 payload는 write 0이다.
- 평가 인프라 실패를 품질 통과·실패로 기록하지 않는다. 승격 직전 모든 결박과 만료를
  재검증한다.
- 승격과 package-ready 모두 current governance epoch/policy의 latest non-superseded
  `Bound(Approved)`만 사용한다. 같은 revision·review purpose에 더 새 cycle이나 active cycle이
  있으면 과거 Bound receipt의 재사용 write는 0이다.
- package-ready는 exact proposal/candidate/basis/dataset/governance/review/eval/policy/ACL/schema/
  boundary/downstream revisions를 receipt·Pending intent·outbox와 같은 transaction에서
  conditional-check한다. handoff_before, fenced claim, downstream expected revision·idempotency·
  stable read-back을 제공하지 못하면 handoff write는 0이며 acceptance를 production 적용으로
  표시하지 않는다.
- Promotion request는 exact proposal/candidate/governance/review/evaluation/policy/ACL/schema/
  boundary/baseline/target revisions를, Rollback request는 exact current/prior state·prior applied
  lineage·policy/ACL/schema/boundary/target revisions를 state·epoch CAS와 같은 transaction에서
  conditional-check한다. 검증 뒤 drift가 끼어드는 TOCTOU write와 apply_before 뒤 Applied는 0이다.
- Promotion과 Rollback은 모두 `RevisionActivation`으로 exact new revision의 fresh
  BoundaryEnforcementPlan/receipt, drift authorization, optional expiry schedule과 gateway reference
  lifecycle을 탄다. prior revision의 과거 enforcement receipt만 재사용하는 Rollback Applied는 0이다.
- Promotion·Rollback·Quarantine·Deactivate·Compensation·BoundaryLeaseRenewal 혼합 32-way 경쟁은
  `(org, artifact, target, environment, expected_epoch)`마다 한 winner다. slot에는
  non-superseded in-flight가 최대 하나이고 최종 `ServingTargetState`도 하나다. 더 큰 epoch의
  authorized supersession은 과거 winner를 `*Superseded`로 보존하며 stale Applied write는 0이다.
  in-process는 state/epoch CAS, 외부 target은 desired state/epoch CAS와 exact target read-back으로
  증명한다.
- 외부 target의 operation 자격·현재 serving 응답은 fresh exact read-back과 external version으로
  판정한다. 설명되지 않는 out-of-band drift는 actual Observed projection과 DriftOpen을 한 번만
  남기고, 사람 adopt·기존 operation retry/compensation·더 높은 kill-switch 전에는 normal
  promotion/rollback과 desired 자동 재적용이 0이다.
- 해석 불가능한 external read-back은 raw digest/version/generation을 가진
  `UnrecognizedExternalState`로 보존하고 ServingTargetState로 축소하지 않는다. adopt·compensation은
  0이며 exact raw version을 expected로 한 Quarantine/Deactivate만 허용한다.
- epoch>0 external absence는 raw generation-fenced token을 보존하되 Deactivated로만 투영하고
  Uninitialized adopt는 write 0이다. AdoptObserved/CompensateToObserved의 ServingRevision도 정상
  Promotion-equivalent 또는 exact prior-applied/current-known-good gate를 통과하지 못하면 Applied가
  0이며 unknown·unsafe actual은 kill-switch로 닫는다.
- external ServingRevision AdoptObserved는 exact actual·next epoch를 예약한 durable
  `AdoptionPending`과 새 boundary plan/drift authorization/optional expiry를 먼저 쓴다. gateway arm은
  새 TargetGatewaySubject Pending reference도 같은 transaction에 쓴다. 별 terminal transaction이
  same external version/generation과 final enforcement를 fresh read-back해 receipt, Pending→Active,
  desired/epoch, TargetDriftAdopted/DriftResolved를 함께 확정하지 못하면 adoption write는 0이다.
  safe-state adoption만 한 transaction으로 닫을 수 있다.
- native target/source는 revision/content, TTL/ACL과 dynamic authoritative grant 또는 fail-closed
  boundary lease를 exact Pending serving binding으로 한 external-version CAS에 원자 적용하며 Pending
  중 read는 0이다. current authorization을 재검증한 conditional Pending→Active와 exact stable
  read-back 뒤에만 `NativeBoundaryEnforcementReceipt`+Applied 또는
  `SourceBoundaryEnforcementReceipt`+Bound를 함께 남긴다. 이 원자성·self-deny·generation fence·
  read-back을 제공하지 못하는 target/source에는 native arm을 열지 않는다.
- gateway는 every-read target/source attestation의 protected revision/content digest와 activation/
  binding generation이 exact current Active reference와 final receipt에 모두 일치할 때만 allow한다.
  B용 낮은 boundary가 Active인 동안 A revision이 out-of-band로 재등장한 경우, 새 content commit 뒤
  B reference가 아직 Pending인 창, Archived/Releasing reference만 있는 revision의 재등장,
  native Active binding B 아래 revision-only A mutation은 reconciler보다 먼저 모두 read deny다.
  missing/unknown attestation, 같은 generation 재사용, Active reference conflict도 deny이며 그런
  adapter는 production enablement가 0이다.
- in-process는 모든 read에서 authoritative current boundary를 검사한다. gateway arm은 호출 직전과
  terminal 직전에 no-bypass route/config/health와 current boundary version을 fresh read-back하고,
  Pending·Active·Releasing reference가 있는 route의 삭제·완화 CAS는 0이다. UoW12 Pending,
  UoW13 Pending→Active와 A→B old Active→Releasing뿐 아니라 external ServingRevision adoption의
  request→terminal도 같은 lifecycle을 쓴다. `GatewayReferenceSubject`는 TargetGatewaySubject와
  SourceGatewaySubject의 닫힌 합이며 공통 route fence는 두 subject를 모두 센다. source gateway
  arm은 UoW7 Pending, UoW8 Bound 뒤 serving이면 Pending→Active/old Active→Releasing, verified
  non-serving 또는 BindingFailure이면 fenced cleanup 뒤 Releasing을 쓴다. failed/superseded attempt
  fence 뒤 cleanup, exact 미서빙+replacement/kill/deny 보호 뒤 Archived를 32-way 경쟁에서도
  보장한다. target RevisionActivation, target adoption, source binding 각각의 동일 protection-scope
  32-way terminal 경쟁은 partial unique Active index와 expected reference revision CAS로 한 winner만
  남기며, 두 Active 또는 stale terminal이 관측되면 allow/write는 0이다. late mutation 가능성이
  있으면 release는 0이다. static copied ACL·webhook·scheduler만
  제공하는 composition은 serving/binding write가 0이다.
- bounded `ServingBoundaryLease`는 policy가 허용한 classification/max lag에서만 쓰고, 매 renewal이
  current policy revision·boundary digest·issuer authorization·optional declassification expiry를
  slot epoch와 함께 CAS한다. drift·정책 철회·만료 뒤 renewal은 0이고 기존 lease는 valid_until 뒤
  자체 deny한다.
- source binding도 exact continuous enforcement와 source arm
  `BoundaryDriftActionAuthorization` 없이는 BindingPending write가 0이다. target drift action은
  shared slot saga, source drift action은 source-owned expected-revision/idempotent/fenced adapter와
  stable read-back만 사용한다. source gateway reference가 non-Archived인 route/config를 target-only
  fence로 완화하거나 BindingFailure/SourceDenyReads/SourceUnpublish의 stable exact read-back 전에
  Archived로 옮기는 write는 0이다.
- rollback은 현재 정책을 통과한 exact prior version으로만 가며 승격 이력을 지우지 않는다.
  적격 prior가 없으면 사람 권한의 quarantine/deactivate로 serving을 fail-closed한다.
- 모든 active cycle에는 assignee와 SLA가 있고 terminal 또는 Superseded로 닫힌다.
- terminal Question Request는 검토·개선 때문에 부활하지 않는다.
- immutable audit export로 revision→review→finding basis→proposal→dataset reservation→candidate
  revision→evaluation→promotion/rollback/quarantine/deactivate/compensation/boundary renewal·drift/
  target drift·adopt/source reconciliation 또는 package handoff 계보를 재현할 수 있다.

## 대안과 기각 사유

- **AI가 검토 뒤 바로 반영** — 권한·자기확증·prompt injection 위험 때문에 기각.
- **모든 AI 답을 무조건 사전 사람 검토** — 기존 위험 기반 ApprovalPolicy를 대체하고 저위험
  운영 가용성을 해치므로 공통 개선 문맥의 규칙으로는 기각. 고위험 정책에서 선택할 수 있다.
- **기존 StageReview·Approval·Reeval을 하나의 범용 상태로 교체** — 각 도메인의 binding 의미와
  복구 규칙이 달라 dual SSOT와 대규모 회귀를 만든다. adapter+receipt로 연결한다.
- **운영 Precedent·답을 자동 골든 라벨로 사용** — 시스템 출력을 정답으로 삼는 자기확증이라
  기각.
- **롤백 시 Promotion을 RolledBack으로 변경** — 역사적 승격 사실과 현재 serving 상태를 섞으므로
  기각. 불변 receipt와 `ServingTargetState`를 분리한다.

## 결과

제품 요구사항은 책임 있는 질문 종결 계층을 유지하면서, 그 결과와 조직 산출물을 안전하게
개선하는 두 번째 루프로 확장된다. 이 ADR만으로 해당 코드가 구현된 것은 아니다. 검토 건수나
AI 제안 수는 성공 지표가 아니다. AI finding의 사람
수용 정밀도·재현율, 사람 검토 시간, 독립 holdout 변화, 승격 뒤 escape defect, rollback률과
복구시간을 baseline과 비교한다. 이 지표는 개선 관찰값이며 개인 고과나 자동 승격 조건으로
쓰지 않는다.
