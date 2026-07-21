# ADR 0063 — S1c.4 production source integration enablement gate

- 상태: Proposed
- 날짜: 2026-07-20
- 선행: ADR 0049·0050·0053, ADR 0059~0062
- 범위: real source adapter를 열기 전에 필요한 production durability, external authority/config, service identity·secret/mTLS와 multi-instance gate
- 제외: fake integration 결과의 production 승격, proposal, evaluation, promotion, serving target state write

## 맥락

S1c.1~S1c.3은 trusted fake integration에서 Pending, BoundV8 및 every-read deny 모델을 결정론적으로 검증한다. SQLite single-process, opaque fake credential ref, fake profile/session은 real source endpoint, network identity, secret rotation, PostgreSQL concurrency, multi-instance lease/outbox/recovery를 증명하지 않는다. profile DTO나 test key를 production registry로 주입하는 것은 ADR 0059의 central Authority 원칙을 우회한다.

## 결정

### 1. enablement는 별 production capability다

real source adapter는 existing fake registry/session을 바꾸거나 flag로 승격하지 않는다. `bootstrap_authorized_production()`이 다음 production dependencies를 모두 live validate한 경우에만 single-use `ProductionSourceIntegrationCapability`을 발급한다. 하나라도 없거나 drift하면 `production_adapters_unavailable`이며 real apply/Bound/read는 0이다.

- HA/backup/recovery가 운영 검증된 PostgreSQL durable registry, outbox, worker lease/fence, profile/revocation/kill audit store와 multi-instance conditional-write isolation
- 중앙 Authority가 소유·승인·감사하는 immutable `ProductionSourceIntegrationProfile` registry. profile change/disable/rotate/kill은 human-authorized versioned command이며 caller, HTTP/MCP body, adapter, DB seed가 직접 쓰지 못한다.
- workload service identity, mTLS client certificate reference and server trust/pinning policy, source-side service account authorization, network egress/no-bypass gateway policy
- secret manager/HSM의 use-only `ExternalSecretHandle`, rotation/revocation event source, break-glass/kill governance. raw secret, certificate private key, bearer token, source body는 DB/audit/outbox/exception/DTO에 남지 않는다.
- source vendor가 문서화하고 contract test로 확인한 expected-revision CAS, semantic idempotency, worker fencing(or gateway proxy proof), stable signed readback, continuous enforcement/every-read attestation, revocation/deny convergence

production capability는 bootstrap attestation의 exact registry revision, PostgreSQL DSN identity/fingerprint, service identity, approved profile/version/digest, secret-handle generation, gateway topology, Authority policy epoch을 bind하고 lifecycle close/revoke에서 즉시 닫힌다.

### 2. external authority/config prerequisites

다음은 code 구현으로 대체할 수 없는 조직/외부 결정이다. 미결정이면 profile은 disabled다.

- source Owner, data owner/security/legal approver, on-call reconciliation owner와 kill 권한자
- source vendor CAS/idempotency/fencing/readback/attestation contract, rate/retention/region/DLP/tenant boundary, incident contact·SLA·rollback/deny runbook
- service account provisioning, mTLS issuance/renewal/revocation, secret-manager policy and audit, egress allowlist and no-bypass gateway/network control
- PostgreSQL HA/backup/restore, migration/change-control, multi-region/tenant isolation boundary, outbox delivery/retry/dead-letter and recovery SLO
- profile activation change request, independent security review, staged shadow/canary and human production enablement approval

이 정보는 source profile의 secret-free approval/reference/digest로만 결박한다. 실제 credential 또는 source body를 receipt에 복사하지 않는다.

### 3. code responsibilities after prerequisites

`ProductionSourceIntegrationRegistry`는 capability-owned port만이다. current approved profile을 resolve하고 workload identity + secret handle로 operation별 `ProductionSourceIntegrationSession`을 열며 adapter/key/endpoint callback injection을 받지 않는다. session은 mTLS·peer identity를 검증하고 source operation idempotency/fence를 결박하며 body-free signed readback/attestation을 내고 profile/secret/Authority revoke에서 닫힌다.

PostgreSQL UoW uses durable lease epoch, outbox claim and terminal conditional insert across instances. external calls remain outside database transactions; before call, terminal and every read it rechecks current Authority/profile/lease/secret-generation. any inability to prove external CAS, signed stable readback, Active enforcement or deny convergence is unavailable/deny, never a fallback to fake or static ACL.

fake `SourceIntegrationProfile`, fake session, fake readback/attestation, v7/v8 fake Bound terminal and their test keys are separate types/namespaces and cannot satisfy production capability validation or seed/migrate a production profile. A fake Bound is not a production permit, source mutation proof, credential grant or profile activation evidence.

### 4. rollout and revocation

enablement sequence is: disabled registry row → validated external prerequisites → shadow contract test with no source visibility → human approval → limited canary profile → continuous observability/kill drill → explicit broader enablement. Each transition is a new profile version and does not mutate past v8 evidence.

Authority revoke, security/legal boundary change, service identity/secret rotation failure, source contract violation, kill command or profile disable immediately blocks new apply/Bound/read and orders gateway/native deny. If external deny confirmation is unavailable, reads remain denied and reconciliation/escalation opens. recovery may retry only the same idempotency key/fence under current approved profile; it cannot silently switch profile or replay a fake observation.

### 5. required schemas and ports

```text
PostgreSQL production_source_integration_profiles
  profile/version/digest, scope, approved service identity/secret-handle refs,
  external contract digest, lifecycle, activation/disable/kill approval refs

PostgreSQL production_source_integration_events
  immutable profile activation/rotation/revocation/kill/reconciliation audit/outbox

PostgreSQL production_source_worker_leases / delivery claims
  org-scoped lease epoch, fence, recovery/dead-letter lineage

ProductionSourceIntegrationRegistry
  open(profile_id, ProductionSourceIntegrationCapability) -> ProductionSession | Unavailable
  current(profile_id) -> ApprovedProfile | Disabled | Unavailable

ProductionSourceIntegrationSession
  apply(SourceApplyRequest, lease_fence) -> SignedProductionReadback | Uncertain
  attest_read(BoundV8Ref) -> SignedProductionServingAttestation | Denied | Unavailable
  close_on_revoke() -> None

ProductionSecretResolver
  open_use_only(ExternalSecretHandle, workload_identity) -> ephemeral session material | Unavailable
```

Schema values are references/digests/identities only; no raw secret or source body. PostgreSQL registry replaces neither Authority policy registry nor fake SQLite evidence; both must exact-bind at use time.

## enablement gate tests and demonstrations

- integration contract tests against a controlled non-production source prove CAS/idempotency/fence, mTLS peer identity, signed readback/attestation, Pending self-deny/no-bypass and revoke/kill convergence; fake classes cannot pass these tests.
- multi-instance PostgreSQL chaos verifies lease/outbox single winner, process crash/restart, duplicate delivery, network timeout/late commit, profile/secret rotation and terminal/read deny across nodes.
- secret scan/redaction proves no raw secret/certificate/body in DB, logs, audit, outbox, metrics or errors; use-only handle cannot be serialized as credential material.
- source Owner/security/legal approval, change-control, shadow/canary, incident/rollback/kill drills, backup/restore and tenant isolation evidence are reviewed before profile enablement.
- wrong service identity/mTLS peer/profile version/secret generation/Authority epoch/CAS/readback/attestation/enforcement or any missing external prerequisite leaves capability unavailable and real source mutation/read authorization 0.

## 결과

ADR 0063은 real adapter 배포 권한이 아니라 enablement prerequisite다. 모든 external/durability gate가 충족되고 사람이 approved profile을 명시적으로 enable하기 전까지 가능한 source path는 fake/testing 경계뿐이며 required result는 `production_adapters_unavailable`다.
