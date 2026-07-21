# ADR 0053 — Tenant Operational Source와 중앙 운영 capability

- 상태: 제안(Proposed)
- 날짜: 2026-07-19
- 계보: ADR 0050의 중앙 Authority 조직 결박과 ADR 0051의 운영 MCP 경계를, ADR 0052의 durable transaction 방향으로 구체화한다.
- 적용 범위: central operational application의 Registry/graph, session, audit reader/writer, HITL source provenance와 org-scoped SQLite conformance adapter.
- 제외 범위: legacy source backfill·dual-write·자동 이관, operational mutation+audit 원자 UoW(R1), actual PostgreSQL adapter·OIDC·production FastAPI 공개·outbox consumer·다중 인스턴스 보장.

## 맥락

독립 재감사에서 기존 `Registry`, `SessionStore`, JSONL/InMemory audit, `HitlToggleMap`, graph callback은 조직 provenance를 저장하거나 validate하지 않음이 확인됐다. configured `org_id`와 Python object identity를 조합한 정적 wrapper는 실제 row scope, provenance, source fault를 증명하지 못한다. 따라서 이런 raw legacy source를 central operational capability로 승격하면 다른 조직 데이터 혼입을 필터링으로 숨기거나 권한 allow로 정당화하는 오류가 생긴다.

ADR 0051의 공통 application·중앙 action·현재 `ResourceRef`·명령 직전 재인가 원칙은 유지한다. 단 generic composition proof의 의미를 실제 tenant source adapter가 발급하는 current validate-only capability로 좁힌다.

## 결정

central operational application은 raw `Registry`, legacy `SessionStore`, audit callback/log, HITL map, graph callback, `(reader, writer)` tuple 또는 self-reported snapshot을 받지 않는다. 구성은 다음 여섯 source-specific, org-bound port의 완전한 `OperationalCentralDependencies`를 요구한다.

- `registry`: 현재 tenant 카드·Owner·admission/transfer source
- `graph`: 같은 registry snapshot에서만 파생되는 graph projection
- `session`: `(org_id, session_id)` identity의 현재 session read/end source
- `audit_reader`: tenant partition의 ordered safe record read source
- `audit_writer`: tenant partition의 safe append target source
- `hitl`: `(org_id, agent_id)` identity의 explicit toggle source

각 port는 source object identity에 exact 결박된 `validate_scope()`를 제공한다. 이 검증은 configured 문자열 또는 composition-time frozen digest가 아니라 실제 source의 current tenant provenance, schema/connection health, row scope 및 decode/integrity를 validate-only로 확인한다. query는 read 직전과 DTO 반환 직전, mutation은 authorization/approval 뒤와 실제 write 직전에 필요한 port를 다시 검증한다. own write 뒤 정상 revision 변화는 다음 command를 막지 않지만 source swap, foreign/mixed row, malformed provenance, schema/decode fault, proof drift는 unavailable 또는 write 0이다. reader와 writer는 별 source이며 서로의 tuple identity로 증명하지 않는다.

새 SQLite conformance adapter는 legacy table을 바꾸지 않는 별 namespace에 다음 canonical tenant tables만 설치한다.

- `operational_registry_state(org_id, revision, payload_json, payload_digest, updated_at)`
- `operational_sessions(org_id, session_id, user_id, status, started_at, last_active_at, revision)`
- `operational_audit_records(org_id, seq, record_json, record_digest, created_at)`
- `operational_hitl_toggles(org_id, agent_id, on, explicit, revision, updated_at)`

registry state는 strict validated tenant graph snapshot과 CAS를 사용하며 graph port는 같은 snapshot에서만 만들어진다. 기존 YAML/Registry journal/legacy session/JSONL/HITL 값은 import·backfill·dual-write source가 아니며 raw legacy central composition은 unavailable로 남는다. tenant provisioning은 별 명시 bootstrap/승인 경계에서 org-tagged strict seed로만 수행한다.

`OperationalSourceCapabilities`와 `OperationalCentralDependencies`는 exact type·complete kind·same configured org·bound source identity를 검사한다. 하나라도 없거나 raw source/partial aggregate가 들어오면 application/MCP는 legacy fallback 없이 unavailable이다. `web.create_app`은 arbitrary proof factory를 public argument로 받지 않는다.

SQLite capability는 결정론·single-process conformance용이다. production bootstrap은 PostgreSQL tenant adapter와 R1 durable mutation receipt/audit/outbox UoW가 모두 조립되기 전까지 이를 production operational capability로 인정하지 않고 `production_adapters_unavailable`로 닫는다.

R1 이전 R2b mutation은 scope/authorization/approval check를 보강할 뿐 state write와 procedural audit append의 atomicity를 주장하지 않는다. R1은 이 source ports의 UoW factory 위에서 mutation, immutable receipt, secret-free audit intent, outbox intent를 하나의 transaction으로 대체한다.

## 결과와 한계

중앙 운영 MCP가 “principal의 org 문자열만 맞는 legacy source”를 실제 기업 tenant source로 오인하지 않는다. source provenance를 확보한 새 operational data path만 중앙 자동화에 연결할 수 있다. 반면 이 결정은 legacy demo/UI를 자동으로 tenantize하지 않고, SQLite가 PostgreSQL production/다중 인스턴스 보장을 뜻하지 않으며, audit append 실패 뒤 state-only가 남지 않는다는 보장은 R1 전까지 제공하지 않는다.
