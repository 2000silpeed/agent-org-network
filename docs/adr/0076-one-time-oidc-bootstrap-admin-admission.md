# ADR 0076 — 일회성 OIDC Bootstrap Admin admission

- 상태: Accepted (RB3.2a component 구현됨; 독립 review·최종 gate·지원 수준 승격은 미완료)
- 날짜: 2026-07-31
- 관련: ADR 0004 (중앙 Authority), ADR 0005 (Registry graph), ADR 0064 (Registry User admission), ADR 0075 (세 installation artifact와 Next 이관)

## 맥락

RB3.1a Central Question Intake는 구성된 조직에 Registry User가 하나도 없으면 fail-close한다. 그러나 첫 Registry User를 demo seed, raw SQLite, CLI의 user/email 자기보고로 만들면 신원과 Authority의 중앙 경계가 무너진다. 반대로 Central Next SSO 화면을 먼저 열면, 아직 별 artifact인 Central Next와 Owner-local Next의 경계 및 browser onboarding 범위가 불명확해진다.

## 결정

RB3.2a는 browser UI를 열지 않는 `aon-central bootstrap-admin` 한 번의 Central Server admission으로 한정한다. command shape는 정확히 다음과 같으며 `--profile`과 `--attestation` 외 user, email, OIDC subject, role, manager, token 또는 secret 인자를 받지 않는다.

```text
aon-central bootstrap-admin --profile CENTRAL_PROFILE --attestation BOOTSTRAP_ADMIN_ATTESTATION
```

Central Next와 Owner-local Next는 서로 다른 frontend/backend installation 경계다. 이 command는 Central API/CLI 경계만 사용하며 Central Next를 열거나 Owner API·Owner-local Next를 호출하지 않는다. 현재 `frontend/` Developer Reference는 이 command의 UI나 제품 artifact로 승격하지 않는다. 향후 Central Next와 Owner-local Next는 별 deployable artifact/package가 되지만, 이 단계에서는 디렉터리명을 강제하지 않는다.

### Bootstrap Admin Attestation

`BOOTSTRAP_ADMIN_ATTESTATION`은 읽기 전용 JSON/YAML profile이며 정확히 아래 field만 가진다. 문자열 reference는 기존 opaque-reference 문법, digest는 lower-case SHA-256 hex, revision은 정수 `0`이다. `manager_id`는 field가 아니라 항상 `null`로 고정된다.

| field | 의미 |
|---|---|
| `schema_version` | 정수 `1` |
| `attestation_id` | one-time opaque attestation reference |
| `org_id` | Central profile의 configured org와 exact match |
| `registry_user_id` | 최초 root User의 opaque Registry User ID |
| `oidc_provider_id` | Central profile의 provider reference와 exact match |
| `oidc_issuer_digest` | configured issuer의 canonical digest |
| `oidc_audience_digest` | configured audience의 canonical digest |
| `oidc_subject_digest` | verified OIDC `iss`와 `sub`의 canonical digest |
| `verified_email_digest` | verified OIDC email의 canonical digest |
| `device_authorization_ref` | device authorization transaction을 식별하는 opaque reference |
| `idempotency_key` | Registry admission replay key |
| `expected_registry_revision` | 정수 `0` |
| `authority_policy_digest` | current Authority snapshot digest와 exact match |

Attestation, normal CLI stdout/stderr, audit/outbox, receipt projection, HTTP response와 support
log에는 raw email, raw OIDC claim, id/access/refresh token, device code, user code 또는 client
secret를 넣지 않는다. verification URI와 one-time user code는 예외적으로 invoking process의
controlling terminal `/dev/tty`에만 one-time 표시한다. 이는 stdout/stderr redirection을 통과하지
않으며, `/dev/tty`를 열 수 없는 non-TTY 실행은 network/write 전에 fail-close한다. 기존
`SqliteProductionRegistryUsers`는 Registry User의 canonical email을 내부 durable record로
필요로 하므로, adapter가 검증 뒤 process memory에서만 command에 공급한다. 그 email은 CLI
input·attestation·receipt/audit/outbox egress가 아니다.

### OIDC device authorization port

RB3.2a implementation은 `BootstrapOidcDeviceAuthorizer` port를 둔다. production adapter는 configured issuer의 device authorization endpoint/client ID/scope로 device authorization grant를 수행하고, token endpoint result를 existing `OidcProvider.verify`로 다시 검증한다. 테스트는 결정론 fake adapter를 주입한다. port result는 메모리에만 존재하는 `VerifiedBootstrapIdentity(issuer, audience, subject, email, email_verified)`이며, attestation의 digest와 configured provider/issuer/audience를 모두 exact match해야 한다.

Central installation profile은 exact field로 `bootstrap_oidc_device_authorization_url`,
`bootstrap_oidc_device_client_id`, `bootstrap_oidc_scope`를 가진다. 이 값은 endpoint/public
client reference/scope이지 user, email, token 또는 client secret가 아니다. interactive
verification URI와 one-time user code는 stdout/stderr가 아니라 `/dev/tty`에만 one-time 표시하고,
non-TTY에서는 fail-close하며 어느 durable record나 log에도 기록하지 않는다.

### Authority와 durable admission

Bootstrap은 Authority 우회가 아니다. attestation의 `registry_user_id`는 current Authority snapshot의 subject-role binding으로 해석되고, 그 binding은 central `user.register`를 허용해야 한다. `BootstrapAdminRegistrationAuthorizer`는 transaction 안에서 다음을 전부 확인한 경우에만 existing `CurrentUserRegistrationAuthorization`을 반환한다.

1. Central schema와 bootstrap schema가 read-back 가능한 상태다.
2. configured org, provider, issuer/audience digest, OIDC subject/email digest, policy digest와 command이 attestation에 exact match한다.
3. current Authority가 candidate Registry User ID의 `user.register`를 허용한다.
4. Registry revision은 최초 write의 `0`이거나, 이미 존재하는 **동일 command digest**의 replay다.

그 후 existing `SqliteProductionRegistryUsers.register`를 그대로 호출한다. 따라서 immutable receipt, audit, outbox, policy evidence, semantic idempotent replay와 Registry graph validation을 재구현하거나 raw SQL로 우회하지 않는다. 최초 User는 `manager_id=None`인 root User다.

`central_bootstrap_admin_seals`는 Central DB의 별 durable schema다. seal은 `org_id`, `attestation_id`, `attestation_digest`, `registry_user_id`, registration command digest, Authority policy digest와 `sealed_at`만 저장하고 immutable trigger를 가진다. raw claim/email/token은 저장하지 않는다. Registry registration이 commit된 뒤 seal write 전에 process가 죽어도, 동일 attestation/device identity의 replay가 existing Registry receipt를 read-back한 뒤 seal을 완성한다. 다른 attestation, 다른 identity, 다른 digest 또는 다른 revision은 seal을 만들 수 없다.

### 상태·오류·동시성

| 상태 | 정의 | 허용 동작 |
|---|---|---|
| `Unmigrated` | Central marker 또는 Registry/Question/bootstrap schema capability의 marker-last read-back이 없다 | `bootstrap-admin`은 write 없이 unavailable로 종료 |
| `BootstrapPending` | schema capability는 유효하지만 configured org의 immutable bootstrap seal이 없다 | exact attestation/device identity의 첫 admission 또는 crash-replay만 허용 |
| `BootstrapSealed` | immutable seal, matching Registry receipt/audit/outbox, root User와 current Authority evidence가 read-back된다 | bootstrap 재실행은 same command의 replay만 성공; 새 최초 admission은 거부 |

`SqliteProductionRegistryUsers`의 `BEGIN IMMEDIATE`, receipt idempotency와 Central seal의 exclusive transaction을 사용한다. 동시 동일 command는 하나의 Registry User/receipt/audit/outbox와 하나의 seal로 수렴한다. 같은 replay key의 다른 command, policy/attestation drift, unexpected unsealed Registry mutation, fault read-back 실패 및 corrupt seal은 write 없이 conflict 또는 unavailable다. stable exits는 configuration `78`, denied/attestation or Authority mismatch `77`, replay conflict or sealed-other `75`, unavailable/fault/not-ready `69`, success/replay `0`이다. 공개 출력은 status, opaque attestation/user reference, revision, receipt/seal digest만 허용한다.

### 범위와 후속 경계

RB3.2a 완료 기준은 one-time OIDC admission의 durable root User, restart read-back, same-command replay, conflicting replay/identity/policy denial, crash recovery와 concurrency safety다. browser route, session cookie, JIT provisioning, invitation, additional Registry User onboarding, Agent Card admission, routing 또는 Question User MCP end-to-end는 이 slice에 없다.

RB3.2b는 Central Next SSO와 **추가** Registry User onboarding을 별 slice로 유지한다. 그것은 Central Next/API의 별 process boundary에서만 동작하며, Owner-local Next/API와 artifact를 합치거나 raw source/full draft/Owner credential을 Central browser BFF로 중계하지 않는다.

## 결과

Central Question Intake의 Registry bootstrap blocker를 demo 없이 실제 durable admission으로
해소한다. 그러나 이는 하나의 Central component slice의 evidence다. support status는
`tested_component_factory`/`product_target_not_available` 경계를 유지하며
`runnable_local_reference` 또는 production/pilot로 승격하지 않는다. 독립 code review, 최종
gate와 test OIDC Manual Acceptance는 이 ADR의 구현 사실과 별개로 TASK에서 닫는다.
