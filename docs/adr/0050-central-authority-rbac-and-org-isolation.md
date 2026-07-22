# ADR 0050 — 중앙 Authority·RBAC·조직 격리

- 상태: 채택(Accepted)
- 날짜: 2026-07-14
- 계보: ADR 0004의 중앙 Authority, ADR 0016·0021의 인증 경계, ADR 0042~0048의 Request-first 수명주기, ADR 0049의 readiness-only production 조립을 P17.8 권한 계약으로 잇는다.
- 적용 범위: 중앙 정책 스냅샷, 인증 principal, default-deny RBAC, 단일 조직 프로세스 격리, 워커 연결·회신 권한, production 조립 capability.
- 제외 범위: 실 IdP discovery·JWKS 네트워크·authorization code/PKCE, 정책 hot reload, 다중 조직 공유 프로세스, mTLS, durable policy epoch·WorkTicket lease/fencing·outbox·재시작 복구(P17.9), production FastAPI 앱과 포트.

## 맥락

현재 production bootstrap은 demo와 legacy 조립을 분리했지만 실제 OIDC·Authority/RBAC 어댑터가 없어 항상 unavailable로 닫힌다. 질문 principal과 사람 처분 principal은 주로 `org_id·subject_id`만 담고, 기존 운영 웹에는 인증 OFF와 무비밀번호 세션 호환 경로가 남아 있다. Owner·Manager·Approver 귀속 검사는 있어도 중앙 역할 권한과 결합되지 않은 표면이 있다. monitor·org graph·console·admin·authoring은 “로그인한 사용자”만으로 열리는 곳도 있다.

워커는 연결할 때 admission token의 Owner와 등급을 확인하지만, token store가 없으면 개발 호환을 위해 fail-open한다. 더 중요한 문제는 `SubmitAnswer`가 연결 principal을 `dispatcher.submit`에 넘기지 않는다는 점이다. ticket을 어느 연결에 보냈는지, 현재 Agent Card Owner가 누구인지, 연결이 끊겼다가 새 epoch로 교체됐는지를 답 저장 직전에 다시 검증하지 않는다.

P17.8은 이 경계를 코드로 닫되 P17.9의 내구 transaction·lease를 앞당기지 않는다. 기존 demo 앱에 production flag를 붙이거나 FastAPI 앱을 먼저 여는 방식은 ADR 0049와 충돌하므로 사용하지 않는다.

## 결정

### 1. production은 한 조직에 결박된 프로세스다

P17.8 production 조립은 설정된 `org_id` 하나만 받는다. principal, 정책, 리소스와 저장 객체의 조직이 모두 그 값과 같아야 한다. 다중 조직을 한 프로세스에서 처리하는 모델은 P17.8 범위가 아니다.

다른 조직, 미지 리소스와 권한 없는 리소스 조회는 공통 `NotFoundOrDenied` 의미로 숨긴다. 조직 불일치에서 존재 여부를 노출하지 않고 read/write를 모두 0으로 둔다.

### 2. 역할은 IdP가 아니라 중앙 정책이 정한다

production 질문과 운영 표면에는 회사 OIDC로 검증된 신원이 필요하다. issuer·audience·만료·nonce/state 검증을 통과한 claims를 Registry User에 매핑한 뒤에만 인증 principal을 만든다. IdP의 role/group claim은 P17.8 권한의 직접 원천으로 쓰지 않는다.

초기 역할은 다음 일곱 개로 분리한다.

```text
requester | owner | manager | approver | operator | auditor | admin
```

역할 배정과 권한은 중앙 정책 스냅샷이 소유한다. Agent Card, HTTP body·path·query, MCP 인수, 워커 프레임이 역할이나 조직을 늘릴 수 없다.

### 3. 중앙 정책은 엄격한 시작 스냅샷이다

정책 원천은 배포 시 읽기 전용으로 마운트한 versioned YAML(`routing_rules.yaml` 또는 동등한 주입 원천)이다. P17.8에서는 시작할 때 한 번 읽고 실행 중에는 바꾸지 않는다.

```text
AuthorityPolicySnapshot
  schema_version
  org_id
  policy_version
  content_sha256
  subject_roles[]          # subject_id → roles
  role_permissions[]       # role → actions
  route_rules[]            # intent/Agent Card 중앙 Authority
  worker_bindings[]        # credential/Owner/role/generation
```

파서는 unknown key·role·action, 중복 subject/permission/binding, 빈 값, 다른 조직, 잘못된 version·digest를 거부한다. 정규화한 정책 내용의 SHA-256을 검증하고 caller가 제공한 digest를 그대로 신뢰하지 않는다. 누락·파싱 실패·미지 action·의존성 오류는 모두 deny이며, 권한 없는 상태에서 도메인·저장 부수 효과는 0이다.

hot reload는 정책 교체와 도메인 write 사이의 TOCTOU를 만들 수 있으므로 P17.9 이후 durable policy epoch와 transaction 설계로 넘긴다.

### 4. 인증과 권한 결과를 sealed 타입으로 전달한다

```text
AuthenticatedPrincipal
  org_id
  subject_id
  identity_provider
  identity_session_id

ResourceRef
  org_id
  kind
  resource_id?
  owner_subject_id?

AuthorizationGrant
  org_id
  subject_id
  action
  resource
  roles
  policy_version
  policy_digest

AuthorizationDenied
  kind = "not_found_or_denied" | "policy_unavailable"

CentralAuthorizer.authorize(principal, action, resource)
  -> AuthorizationGrant | AuthorizationDenied
```

타입은 frozen·strict·extra-forbid다. grant는 exact org/action/resource/policy version/digest에 결박한다. 애플리케이션 경계는 grant 필드와 현재 리소스를 다시 대조하며, 다른 명령이나 리소스에 재사용하지 않는다. authorization은 도메인 전이가 아니고 audit도 authorization이나 전이를 대신하지 않는다.

### 5. RBAC와 동적 귀속은 AND 조건이다

역할 permission만으로 리소스 소유권을 건너뛸 수 없고, 현재 지정자·후보·Manager 귀속만으로 중앙 RBAC를 대신할 수도 없다.

`ResourceRef.org_id`는 principal과 중앙 policy의 조직 결박이지, 임의 운영 데이터 원천의 조직 provenance 증명은 아니다. central operational capability는 Registry/graph, session, audit/monitor, HITL처럼 실제로 소비하는 source별 composition-owned scope proof가 있을 때만 조립한다. proof 없음·drift·다른 조직 행 혼입·source instance 교체는 권한 allow로 보정하거나 부분 필터링하지 않고 unavailable 또는 대상 비노출로 닫는다.

- requester: `question.create`, 기존 `Received(revision=0)` 자기 질문의 `conflict.open`, 자기 질문의 `question.read|stream`, 자기 답의 `answer.correction.read|feedback.write`. `conflict.open`은 정책이 다른 역할에 잘못 permission을 부여해도 requester role만 grant할 수 있고, server-side Request resolver가 org·request_id·requester·Received·revision 0을 exact 읽기 전에는 arbitrary `ResourceRef`에 grant하지 않는다. 대상은 아직 없는 ConflictCase가 아니라 existing `question_request` ResourceRef다.
- owner: 자기 Agent Card·후보 ConflictCase·자기 supervision·authoring, 중앙 permission이 있고 현재 지정된 경우의 Approval
- manager: 중앙 permission과 현재 ManagerItem 귀속을 모두 만족하는 queue/read/act
- approver: 중앙 permission과 현재 Approval assignment를 모두 만족하는 list/detail/decide/reassign
- operator: monitor·org graph·scorecard·session·HITL 운영, 그리고 open durable ConflictCase의 `conflict.escalate`. 이 action은 정책이 다른 역할에 잘못 permission을 부여해도 operator role만 grant할 수 있고, server-side Conflict Case resolver가 exact org·case_id의 durable `open` Case(AwaitingConflict Request 동반)를 읽기 전에는 arbitrary `ResourceRef`에 grant하지 않는다. 대상은 `question_request`가 아니라 이미 존재하는 `conflict_case` ResourceRef다. 자세한 계약은 §11과 ADR 0065다.
- auditor: audit·monitor의 read-only 표면
- admin: Agent Card 등록·Owner 이전, 워커 credential 발급·revoke, 정책 관리 준비 표면

admin, operator와 auditor는 합치지 않는다. mutation 직전에는 저장 Item/Case/Request와 현재 정책 snapshot을 다시 읽어 permission·조직·동적 귀속을 대조한다.

### 6. 모든 production command/query는 action manifest에 등록한다

보호 대상은 HTTP route 목록이 아니라 애플리케이션 command/query다. HTTP·SSE·MCP가 같은 action을 호출한다.

```text
question.create | question.read | question.stream
conflict.open | conflict.escalate
approval.list | approval.read | approval.decide | approval.reassign
conflict.list | conflict.concur | conflict.document.read
manager.list | manager.act
supervision.read | supervision.correct | scorecard.read
monitor.read | audit.read | org_graph.read
session.end | hitl.read | hitl.write
worker_credential.issue | worker_credential.read | worker_credential.revoke
card.read | card.register | card.transfer_owner
author.read | author.write | author.publish
worker.connect | worker.submit | worker.publish_index | worker.sync_knowledge
```

surface manifest 테스트는 production에 노출 가능한 모든 command/query가 정확히 한 action과 authorizer 경계를 가지는지 확인한다. 인증 OFF, 익명 질문 cookie, 무비밀번호 로그인, path/body actor 가장은 demo 호환에만 남기고 production capability에서는 거부한다. static UI도 별 production 앱을 만들 때 인증 뒤에만 mount한다.

질문 생성은 authorization을 Request 생성보다 먼저 수행한다. 미인증·권한 없는 입력은 Question Request가 아니므로 write 0이다. authorization 뒤 Request가 생성된 다음 의존성이 실패하면 기존 미아 없음 계약에 따라 저장된 상태와 SLA를 보존한다.

### 7. 워커 연결과 submit을 exact binding으로 재검증한다

production 워커 admission은 검증된 opaque 단기 token이 필요하며 token store 부재를 허용하지 않는다. token과 정책 binding은 `org_id·owner_id·credential_id·role·generation`을 결박한다.

```text
WorkerConnectionPrincipal
  org_id
  owner_id
  credential_id
  credential_generation
  role
  connection_epoch
```

연결 성공 때 중앙이 principal과 새 process-local `connection_epoch`를 만든다. push 때 ticket에 exact connection principal/epoch와 attempt를 기록한다. submit은 프레임의 자기보고가 아니라 연결 세션 principal을 반드시 dispatcher에 전달한다.

답 저장 직전 다음 값을 현재 저장 상태와 다시 대조한다.

- ticket ID·request ID·attempt·claimed 상태
- ticket org·Owner·Agent Card ID
- 연결 principal의 org·Owner·role·credential generation·connection epoch
- 현재 Registry Agent Card와 `card.owner`
- 현재 Request/route 또는 request-scoped Authority grant
- primary/backup 연결 등급과 현재 delegation

끊긴 연결, reclaim 뒤 예전 epoch, 다른 Owner·role·Agent Card·ticket, revoke된 credential과 변경된 `card.owner`의 submit은 answer write 0이다. 이 fence는 같은 프로세스에서 stale 회신을 막는 계약이다. durable lease·fencing token과 재시작 복구는 P17.9가 맡는다.

### 8. production 조립은 exact capability identity를 검증한다

`QuestionSurfaceComposition`과 `production_bootstrap`은 같은 `AuthorityPolicySnapshot`, `CentralAuthorizer`, OIDC/Registry identity resolver를 주입받아 exact identity와 configured org를 확인한다. canonical production attestation과 wiring seal에도 이 dependency identity를 포함한다.

P17.8이 끝나도 actual env readiness는 P17.9 전까지 `production_adapters_unavailable`로 닫는다. FastAPI production 앱, module-level `app`과 포트는 만들지 않는다. P17.8의 성공 scope는 권한 capability가 포함된 `composition_contract_only`다.

### 9. S5 구현 상태와 process-local 한계

`WorkerConnectionPrincipal`은 `org_id·owner_id·credential_id·credential_generation·role·connection_epoch`를 가진 typed machine identity다. dispatcher는 register 성공 후 socket마다 이를 한 번 캡처하며, submit·publish·sync·disconnect에서 현재 owner/role/epoch mapping과 exact 비교한다. 동일 Owner·role 재연결 뒤의 예전 socket은 새 epoch 권한을 빌릴 수 없고, 예전 socket의 disconnect는 새 연결을 제거하지 않는다.

push는 `ticket·attempt·Agent Card·Owner·connection epoch`를 묶은 `WorkDeliveryBinding`을 남긴다. submit 직전에는 live connection, binding, 현재 Registry card owner와 strict worker action binding을 다시 확인한다. central worker mode는 worker authorizer·principal resolver·Registry가 모두 있을 때만 열리며 일부 주입은 legacy token fallback 없이 거부한다. raw credential은 connection·ticket·audit에 보관하지 않는다.

이는 같은 프로세스의 stale-session fence다. credential issuance/revocation의 durable authority, lease/fencing token, queue·answer·audit UoW, 재시작·다중 인스턴스 복구는 P17.9 이전에 보장하지 않는다. 기존 `DocumentContent` fetch 응답은 이 worker action manifest 범위에 포함되지 않는다.

### 10. S6 production capability 결속

`ProductionAuthorityCapability`은 configuration org, strict policy snapshot, 그 snapshot에서 만든 exact `SnapshotCentralAuthorizer`, identity resolver, operational/worker authorization과 worker binding source를 같은 객체 계보로 결박한다. `bootstrap_authorized_production()`은 실제 `QuestionSurfaceComposition`의 Question Resolution authorizer와 production resolver·operational boundary 결속도 대조한다. 주입 일부 누락, duck object, 다른 snapshot·authorizer, 조립 뒤 binding 대체와 재사용된 capability claim은 모두 조립 전 거부한다.

capability claim과 Question Surface close/revoke는 같은 lifecycle lock으로 직렬화한다. 닫힌 composition은 claim할 수 없고 close가 claim을 revoke한 뒤 재claim도 허용하지 않는다. 이 proof는 trusted Python process 안의 `composition_contract_only`다. 실제 OIDC/JWKS·production FastAPI·port, durable policy epoch, durable worker credential authority와 multi-instance lifecycle은 여전히 범위 밖이다.

### 11. P17.9 durable escalation을 위한 `conflict.escalate` action (2026-07-22·S4.3c.1)

P17.9 S4.3c는 deadlock/Registry drift에 빠진 open durable ConflictCase를 durable하게 Manager로 넘긴다. InMemory ADR 0046은 마지막 상이 표결이 escalation을 자동 seal했지만, durable 전이(Case `escalated`·FromDeadlock ManagerItem·Request `AwaitingManager`)는 되돌리기 어려운 운영 처분이므로 중앙 action과 사람 통제 gate를 명시한다. 이 절은 action/role/resource mapping만 정하고, 승인 증거 lifecycle과 3중 결박은 ADR 0065가 정한다.

- **action:** `conflict.escalate`를 중앙 action manifest에 추가한다. HTTP·MCP·UoW가 같은 action을 호출한다.
- **role hard-limit(operator 전용):** `conflict.open`이 requester로 hard-limit된 선례를 그대로 복제한다. `ACTION_ALLOWED_ROLES["conflict.escalate"] = {operator}`로 두어, 정책이 owner·manager·admin 등에 permission을 잘못 부여해도 operator 외 role은 grant를 받지 못한다. escalation은 owner concurrence(`conflict.concur`)도, Manager 처분(`manager.act`)도 아닌 운영 human-control 처분이므로, `session.end`·`hitl.write`와 같은 operator 계열에 둔다.
- **resource kind(`conflict_case`):** `ACTION_RESOURCE_KIND_REQUIREMENTS["conflict.escalate"] = "conflict_case"`. `conflict.open`의 대상이 아직 없는 Case가 아니라 existing `question_request`였던 것과 대칭으로, `conflict.escalate`의 대상은 이미 durable하게 존재하는 open `conflict_case`다. `ResourceRef.kind`가 `conflict_case`이고 `resource_id`(conflict_id)가 있어야 한다.
- **resolver re-read:** `ResourceRef`는 신뢰하지 않는 입력이므로, 서버 측 Conflict Case resolver가 exact org·conflict_id의 durable `open` Case(그리고 그 Case에 연결된 `AwaitingConflict` Question Request)를 읽어 증명하기 전에는 arbitrary `ResourceRef`에 grant하지 않는다. 이는 `conflict.open`의 `ConflictOpenRequestResolver` re-read와 같은 패턴이다. 단 `conflict.escalate`는 특정 subject 귀속(owner_subject_id == principal)이 아니라 open Case 실재로 동적 결박하므로 `DYNAMIC_SUBJECT_REQUIREMENTS`에는 넣지 않는다.
- **HITL은 AND 2층(R5.4 동형):** 중앙 operator grant는 첫 층일 뿐이다. 두 번째 층으로, 별도 sealed 사람 승인 증거(ADR 0065의 `ConflictEscalationApprovalEvidence`)가 command·resource·escalation cause·graph selection에 exact 결박된 채 취득·재확인돼야 한다. 두 층은 AND이며, `worker_credential.revoke`의 중앙 grant AND `CredentialApprovalEvidence` 2층(R5.4)과 같은 결이다. 기존 답변 HITL toggle(ADR 0025)은 이 escalation 승인이 아니다.
- **경계:** 이 절은 read-only authorization contract만 연다. Case escalated·ManagerItem·Request 전이와 receipt/audit/outbox write는 S4.3c.3 UoW, receipt graph schema는 S4.3c.2 범위다.

### 12. P17.9 durable Manager 처분을 위한 `manager.act` resource-aware 계약 (2026-07-22·S4.4a)

P17.9 S4.4는 open durable ManagerItem(FromUnowned·FromDeadlock)을 durable하게 Assign/Dismiss로 처분한다. §11의 `conflict.escalate`가 open Case를 Manager로 넘기는 operator human-control 처분이었던 것과 달리, `manager.act`는 그 escalated/unowned Item을 *받은* Manager의 1인칭 routine 처분이다. 그래서 별도 사람 승인(HITL) 2층이 아니라 중앙 grant(1층) + 현재 durable 상태 resolver로 못박는다. 이 절은 `manager.act`의 resource-aware read-only authorization contract만 정하고, ManagerItem·Request 전이와 receipt/audit/outbox write는 S4.4b~d UoW 범위다.

- **action(기존):** `manager.act`는 이미 manifest에 있다. HTTP·MCP·durable UoW가 같은 action을 호출한다.
- **role hard-limit(manager 전용):** `ACTION_ALLOWED_ROLES["manager.act"] = {manager}`. `conflict.escalate`가 operator로 hard-limit된 선례처럼, 정책이 owner·operator·admin 등에 permission을 잘못 부여해도 manager 외 role은 grant를 받지 못한다. 처분은 escalation(operator)도 owner concurrence(owner)도 아닌 Manager 1인칭 처분이므로 manager 계열에 hard-limit한다.
- **resource kind(`manager_item`):** `ACTION_RESOURCE_KIND_REQUIREMENTS["manager.act"] = "manager_item"`. `conflict.escalate`가 existing open `conflict_case`를 대상으로 하듯, `manager.act`의 대상은 이미 durable하게 존재하는 open `manager_item`이다. `ResourceRef.kind`가 `manager_item`, `resource_id`(item_id), `owner_subject_id`(item의 manager_subject_id)를 가져야 한다(`conflict_case`와 달리 owner_subject_id 필수 — 처분은 subject 귀속이다). `manager.list`는 `manager_item_collection`이라 resource kind requirement에 넣지 않는다.
- **subject 귀속 유지(1인칭):** `conflict.escalate`와 달리 `manager.act`는 `DYNAMIC_SUBJECT_REQUIREMENTS["manager.act"] = {manager}`를 그대로 둔다. 처분은 그 Item에 귀속된 Manager만 내릴 수 있으므로(1인칭), manager role은 `resource.owner_subject_id == principal.subject_id`일 때만 eligible이다. escalation이 open Case 실재로만 결박한 것과 달리, 처분은 subject 귀속 **AND** state-aware 둘 다다.
- **ManagerItem-state-aware resolver re-read:** `ResourceRef`는 신뢰하지 않는 입력이므로, 서버 측 `ManagerActItemResolver`가 exact org·item_id의 durable `open` ManagerItem(그리고 그 Item에 연결된 `AwaitingManager` Question Request)을 읽어 증명하기 전에는 arbitrary `ResourceRef`에 grant하지 않는다. `ConflictEscalateCaseResolver` re-read와 같은 패턴이다. snapshot은 typed reference·리터럴만 반환한다:

  ```python
  class ManagerActItemSnapshot(_FrozenAuthorityModel):
      org_id: str
      item_id: str
      manager_subject_ref: str          # durable 저장 그대로의 typed ref "subject:<sha256>"
      state_kind: Literal["open"]
      request_state_kind: Literal["awaiting_manager"]

  class ManagerActItemResolver(Protocol):
      def resolve_manager_act_item(self, *, item_id: str) -> ManagerActItemSnapshot | None: ...
  ```

  - **1인칭을 durable 상태에 결박(central·ref-space):** durable manager_item의 `manager_subject_id`는 raw가 아니라 typed ref(`subject:<sha256>`)로 저장되고 principal.subject_id는 raw이므로, resolver match는 `current.manager_subject_ref == "subject:" + sha256(principal.subject_id)`로 ref-space에서 대조한다(central_authority는 이미 `sha256`을 import). 이로써 1인칭이 카드 자기보고가 아니라 durable Item 소유에 중앙 결박된다. `DYNAMIC_SUBJECT_REQUIREMENTS`의 raw `owner_subject_id == principal.subject_id`(UoW가 owner_subject_id=principal.subject_id로 echo)는 coarse gate이고, 이 ref 대조가 durable authoritative 결박이다.
  - resolver는 `source_kind`(unowned/deadlock/dispatch)·`public_kind`(unowned/contested/dispatched)를 보지 않는다 — RBAC는 "이 open Item의 Manager가 처분할 수 있다"만 증명하고, source별 유효성(FromDispatch 거부·FromDeadlock evidence 결박·FromUnowned intent 검증)은 UoW가 판정한다.
- **replay는 이 state-aware 가정에 선다(ADR 0065 §9 note ① 동형):** durable 처분이 성공하면 Item은 `resolved`/`dismissed`가 되어 resolver의 `state_kind == "open"`이 더는 성립하지 않는다. 따라서 같은 canonical command의 replay는 새 grant를 받을 수 없고(resolver `None` → deny), 불변 command receipt가 authorization proof다. S4.4 UoW replay는 중앙 재인가를 재실행하지 않는다 — Case가 escalated라 escalation replay가 재인가를 못 하는 것(ADR 0065 §9 Q3·note ①)과 정확히 같은 구조다. 실 `SnapshotCentralAuthorizer`에 `manager_act_item_resolver`를 배선할 때(P17.9 후속) 이 open-요구 가정을 명세·테스트로 못박아야 한다.
- **하위호환:** 현 InMemory P17.4/deadlock 처분 조립(`question_surface_composition`)은 `central_authorizer=None`(RBAC 우회·legacy `ManagerPrincipal`)이거나 테스트 Fake authorizer라 `SnapshotCentralAuthorizer.authorize("manager.act")` 실경로가 없다. 따라서 resolver gate·resource kind·role hard-limit 추가는 기존 green을 깨지 않는다. 실 authorizer로 manager.act를 authorize하는 곳(durable S4.4 UoW·후속 배선)은 반드시 `ManagerActItemResolver`를 배선한다(미배선 = fail-closed deny).
- **경계:** 이 절은 read-only authorization contract만 연다. ManagerItem open→resolved/dismissed·Request AwaitingManager→ReadyToDispatch/DeclinedRequest 전이와 S4.1 receipt/audit/outbox write는 S4.4b~d UoW 범위다.

## S1 RED 인수조건

- strict fixture가 snapshot으로 정규화되고 같은 내용은 같은 digest를 만든다.
- unknown key·role·action, 중복, 빈 값, 다른 org, version·digest 불일치는 typed failure이며 factory/write가 0이다.
- OIDC 검증 결과의 role/group claim을 바꿔도 중앙 subject-role 매핑 결과는 바뀌지 않는다.
- 미등록 subject, permission 누락, 미지 action, 정책 의존성 예외는 모두 deny다.
- allow grant는 exact org·subject·action·resource·roles·policy version/digest를 보존한다.
- 다른 org/resource/action으로 grant를 재사용할 수 없다.
- role permission과 dynamic owner/assignment 중 하나만 맞으면 deny다.
- question.create authorization이 실패하면 Request Store write는 0이다.
- production capability는 auth OFF·anonymous·passwordless·token store 부재를 거부하고 FastAPI 객체를 만들지 않는다.

## 결과와 한계

이 결정은 Authority 원천을 중앙 정책으로 하나로 모으고, 역할 권한과 현재 리소스 귀속을 함께 확인하며, 워커 회신 시점의 소유권 drift를 막는다. Agent Card는 계속 under-claim만 하고 권한을 자기보고하지 않는다. 권한 거부가 질문을 미아로 만드는 것도 아니다. 권한 전에 거부된 입력은 Request가 아니며, Request 생성 뒤 실패는 기존 수명주기가 닫는다.

정책 파일 자체의 배포 권한·secret rotation, 실 회사 IdP smoke, reverse proxy, mTLS와 다중 인스턴스 보장은 남아 있다. P17.9가 durable policy epoch·transaction·lease·outbox·재시작 복구를 닫기 전에는 production 서버 준비를 선언하지 않는다.
