# ADR 0074 — Card Owner가 strict A2A 1.0 outbound Remote Runtime을 선택한다

- 상태: Accepted
- 기준일: 2026-07-30
- 관련: ADR 0027, ADR 0067, ADR 0072, PRD PR-5a, TASK RB3.2

## 맥락

A2A는 원격 실행 주체가 메시지와 task를 교환하는 공개 프로토콜이다. 이 저장소의 중앙
Authority, 내부 `Agent Card`, Question Request와 Answer Finalization을 대체하지 않는다.
현재 Owner Worker는 `AgentRuntime`을 주입받지만 legacy fixture이고, 설치 가능한
`aon-owner` 제품은 아직 없다.

A2A를 Central Server의 discovery·proxy·자동 등록 기능으로 붙이면 다음 경계를 깨뜨린다.

- Authority는 중앙 정책만 선언한다.
- 원문, full draft와 Runtime credential은 Card Owner에 남는다.
- Runtime 결과는 AnswerCandidate이며 Approval과 Answer Finalization을 우회하지 않는다.
- 외부 self-description은 내부 `Agent Card`나 Owner identity의 증거가 아니다.

참조 사양과 구현 기준은 [A2A Specification](https://a2a-protocol.org/latest/specification/),
[Agent Discovery](https://a2a-protocol.org/latest/topics/agent-discovery/)와
[공식 Python SDK](https://github.com/a2aproject/a2a-python)다.

## 결정

1. A2A는 Card Owner가 local profile로 명시 선택하는 **outbound-only A2A Remote
   Runtime**으로만 도입한다.
2. 첫 상호운용 profile은 protocol version `1.0`, AgentInterface
   `protocolBinding="HTTP+JSON"`인 REST binding, blocking completed text-only다. SDK의
   0.3 compatibility, streaming, polling/resume와 push callback은 사용하지 않는다.
   직접 Message 또는 completed Task의 단일 text-only Artifact만 수용하고 file/raw/URL/data,
   extension과 multi-modal part는 수용하지 않는다.
3. 공식 `a2a-sdk==1.1.1`은 선택 extra로 두고 `A2AInvocationPort` 뒤에서만 import한다.
   SDK 타입을 코어 도메인이나 기존 `AgentRuntime` 포트에 노출하지 않는다.
4. `A2ARemoteRuntimeProfile`은 active Owner Installation binding, 하나의 canonical HTTPS
   service endpoint, Remote A2A Agent Card digest, protocol/interface, opaque OOB credential
   reference와 제한값에 결박된다. profile과 credential은 Card Owner local
   workspace/keychain에만 둔다.
5. Remote A2A Agent Card는 통신 설정을 검증하는 untrusted metadata다. 내부 `Agent Card`,
   Authority, Registry admission, routing, Card Owner identity나 answer source가 아니다.
6. completed text만 `Answer(text=..., sources=(), mode="full")`로 변환한다. 이 Answer는 기존
   `WorkerLogic → SubmitAnswer → AnswerCandidate → ApprovalPolicy → Answer Finalization`
   경로만 통과한다.
7. remote rejection, unavailable, protocol violation은 `A2A Remote Failure`다.
   `SubmitAnswer`나 가짜 Answer를 만들지 않는다. Worker 수신 루프는 redacted code만 기록하고
   계속 실행하며, 기존 dispatcher의 ticket release/timeout/escalation이 Question Request의
   종착을 책임진다.
8. 기존 `runtime_select.select_runtime()`과 `AON_PROVIDER`에는 A2A를 추가하지 않는다.
   향후 paired `aon-owner worker --profile ...` composition root만 local profile loader와
   A2A adapter를 조립한다.

## 보안 경계

- endpoint는 profile에서 직접 고정하며 질문, context, Remote A2A Agent Card, redirect 또는
  task 결과로 변경할 수 없다.
- HTTP client는 TLS hostname/CA 검증, `follow_redirects=False`, `trust_env=False`, bounded
  timeout와 response size를 강제한다.
- 연결 직전 DNS 결과의 loopback, private, link-local, multicast, unspecified, reserved 주소를
  거부한다. 사설망 egress는 별 정책 결정 전까지 허용하지 않는다.
- Agent Card는 same-origin `/.well-known/agent-card.json`에서만 읽고 canonical digest와
  선택된 `1.0` REST interface가 profile과 정확히 맞아야 한다. 자동 refresh, downgrade,
  endpoint drift와 authentication scheme drift는 fail-closed다.
- credential은 opaque local reference로 해석하며 raw secret을 profile, Remote A2A Agent
  Card, request payload 외 필드, audit, outbox, 중앙 저장소와 log에 넣지 않는다.

## 명시적 비범위

- inbound A2A server와 public Agent Card 발행
- discovery crawl, Registry 자동 등록, remote metadata 기반 Authority 변경
- Central Server/Browser Frontend/Developer API의 A2A proxy 또는 federation
- Question User MCP 도구 추가
- Gemini 모델 provider 연동
- 비동기 task ledger, push notification, interactive input, non-text artifact/file 전달

## 검증

- Fast Gate: frozen profile/outcome, exact binding, completed-text mapping, failure no-submit,
  remote metadata 무권한과 secret 비노출.
- Contract Gate: official SDK client path와 test-only exact MockTransport에 감싼 secured
  HTTP client를 사용한 card fetch, digest/interface 검증, authenticated send-message와
  completed-text 응답. 실제 네트워크를 열지 않는다.
- Manual Acceptance: 실제 HTTPS A2A 1.0 service, OOB credential/keychain, pinned card와 별
  process Owner Worker의 성공·downgrade·credential revoke·endpoint failure·escalation.

SDK adapter와 계약 테스트는 `tested_component_factory` 증거다. `aon-owner` artifact와 실제
Manual Acceptance가 생기기 전에는 `product_target_not_available` 또는 production 지원
상태를 올리지 않는다.

## 결과

표준 codec과 task schema는 공식 SDK가 담당하고, AON 고유의 Authority·Owner
credential·endpoint pinning·Answer Finalization 경계는 narrow port와 profile validator가
담당한다. 기능 범위는 작지만 외부 A2A service가 내부 조직 권한이나 최종 답을 자기보고할 수
없다.
