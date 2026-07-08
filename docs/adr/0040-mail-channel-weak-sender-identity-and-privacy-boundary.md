# 메일 채널 — 발신자는 *약신원*이다: 회신 주소로만 쓰고 라우팅 신원/Authority엔 쓰지 않는다

상태: proposed (2026-07-08 · domain-architect 초안 — Phase 15 A축[메일 채널] shape과 함께 제출·tdd 착수 전) · **게이트 내 shape은 `src/agent_org_network/mail.py`**(A1 추출·A2 회신 투영·A3 `MailChannel` 포트+`FakeMailChannel`) · **실 메일 접근(A4/A5)은 게이트 밖·이 ADR이 그 선결 조건을 못 박는다**

- 계보: ADR 0021(SSO/OIDC 신원 binding — verified email 매핑·`email_verified` 가드)의 *대비축*(메일 발신자는 그 검증을 통과 안 한 3번째 신원 출처) · ADR 0009(페르소나별 인증·분리 면)의 신원 출처 목록 확장 · ADR 0022(`NotificationChannel` 채널 중립·포트+Fake·실 어댑터 게이트 밖)의 채널 패턴을 수신 방향으로 확장 · ADR 0006(중앙=단일 MCP 서버·`ask_org` 1급 진입점)·PRD §5("어느 클라이언트에서든 `ask_org`")의 형제 채널 · ADR 0004(Authority 중앙·카드 자기보고 금지) 정합.
- 성격: **되돌리기 어려운 신원·프라이버시 경계 결정.** 어댑터 배관 자체(IMAP/Graph/SMTP transport)는 ADR감이 아니다 — `NotificationChannel`/`Ingestor` 패턴을 그대로 따르면 되고 shape에 자리만 잡으면 된다. ADR감인 것은 **"메일 발신자를 얼마나 신뢰하나"**와 **"실 메일 PII에 언제/어떻게 접근하나"** 두 결정이다. 둘 다 한 번 정하면 되돌리기 어렵고(신원 출처를 늘리는 결정·조직 메일함 접근 스코프), 4대 불변식(특히 Authority 중앙·노출)과 직접 부딪힌다.

## 맥락 — 세 번째 신원 출처가 들어온다

지금 시스템의 신원 출처는 둘이다:

1. **검증된 SSO 세션(ADR 0021)** — IdP가 `id_token` 서명·만료·audience를 검증하고 `email_verified` 가드를 통과한 신원만 `resolve_identity`로 registry User에 매핑된다. *증명된* 신원이다.
2. **웹 익명 guest / MCP 고정 guest(ADR 0009·0006)** — `_WEB_USER`·`mcp_guest`. 아무도 남을 가장 못 하게 신원을 아예 *고정*한다(선택 우회 차단). 신원을 주장하지 않는다.

메일 채널(Phase 15 A축)은 **세 번째 출처 — 이메일 발신자(`From` 헤더)**를 들여온다. 그런데 이건 둘 중 어느 것과도 다르다:

- SSO처럼 *검증되지 않았다*. `From` 헤더는 스푸핑이 자명하다(SMTP는 발신자를 검증하지 않음). `email_verified=True`인 OIDC claim과 근본적으로 다른 신뢰 등급이다.
- 그렇다고 고정 guest처럼 *신원을 주장 안 하는* 것도 아니다. 메일엔 발신자 주소가 실려 오고, **회신은 그 주소로 돌아가야** 한다(웹·MCP는 요청-응답 세션 안에서 답이 돌아오지만 메일은 비동기·주소 기반).

즉 메일 발신자는 "주소는 있는데 검증은 안 된" 어정쩡한 신원 — **약신원(weak identity)**이다. 이걸 검증된 신원처럼 다루면 스푸핑으로 남을 가장해 그 User의 Authority/스코프를 훔칠 수 있고(Authority 중앙 위반), 무시하면 답을 어디로 보낼지 모른다.

추가로 **PII 경계**가 있다. 메일은 Confluence 위키(ADR 0039·조직이 공유 목적으로 쓴 문서)보다 민감하다 — 개인 서신·제3자 정보·조직 밖 발신자가 섞인다. 실 메일함 접근은 되돌리기 어려운 스코프 결정(무엇을 읽나·얼마나 보관하나·owner 동의)이다.

## 결정

### 결정 1 — 이메일 발신자 = 약신원. *회신 목적지*로만 쓰고 라우팅 신원/Authority엔 쓰지 않는다

발신자 주소의 두 쓰임을 **분리**한다:

- **회신 목적지(반송 봉투) — 허용.** `OutboundMail.recipient`는 원 발신자 주소로 채운다. 이건 "답을 어디로 돌려보내나"일 뿐 신뢰 주장이 아니다(우편 반송 주소와 동형). 스푸핑돼도 피해는 "엉뚱한 주소로 답이 감"이지 Authority 탈취가 아니다.
- **라우팅 신원/Authority — 금지(기본).** 발신자를 registry User로 매핑해 그 User의 권한·편집 스코프·담당권을 부여하지 **않는다**. 게이트 내 기본은 MCP 고정 guest 정신 — 메일 질문은 *내용으로만* 라우팅되고(`AskOrg.handle`은 질문 텍스트로 분류·라우팅), 발신자가 "나는 cs_lead다"라고 주장해도 그 주장으로 담당권이 서지 않는다(Authority 중앙·ADR 0004).

`AskOrg.handle`은 무변경이다. 메일 경로가 넘기는 `User`는 기본적으로 **고정 메일 guest**(`mcp_guest`와 동형의 `mail_guest` 자리)이지, 발신자에서 매핑한 User가 아니다.

### 결정 2 — 발신자→User 신원 매핑은 *검증 경유*·게이트 밖·옵트인

발신자 주소를 registry User로 잇고 싶으면(예: 답변 이력을 그 owner에게 귀속) 그건 **검증을 통과해야** 하며 게이트 밖이다:

- 매핑의 baseline은 ADR 0021의 `resolve_identity`를 재사용한다 — 단, 입력이 검증된 `OidcClaims`여야 한다. **raw `From` 헤더를 직접 registry User에 매핑하지 않는다.** 검증 없는 매핑은 스푸핑 = 가장이 된다.
- 실 검증 메커니즘(SPF/DKIM/DMARC 통과 + 발신 도메인이 조직 도메인 + 발신 주소가 검증된 SSO email과 일치하는 allowlist 등)은 **게이트 밖 구현 판단**이다(이 ADR은 원칙만 — "검증 없이는 매핑 없음"). `HttpOidcProvider`가 게이트 밖이듯.
- 미검증/스푸핑 의심/0매칭/모호는 전부 **guest로 폴백**(거부가 아니라 익명 처리 — 질문은 여전히 내용으로 라우팅돼 미아가 안 남는다). ADR 0021의 "0매칭 거부"와 다른 이유: SSO는 *로그인 관문*(거부=진입 차단)이고, 메일은 *질문 채널*(거부하면 질문이 버려져 미아). 그래서 메일은 신원 매핑에 실패해도 질문 자체는 guest로 흘려보낸다.

### 결정 3 — 실 메일 접근(A4/A5)은 게이트 밖·owner 동의·스코프가 선결

실 IMAP/Graph 수신·SMTP/Graph 송신은 실 네트워크·인증·**실 PII**라 게이트 밖이다(`worker.py` 실 WS 셸·`SlackChannel`/`EmailChannel`[ADR 0022] 정신):

- **게이트 내 픽스처는 전부 합성**(실 발신자·실 본문 0 — `FakeMailChannel`에 주입하는 `InboundMail`은 합성 dict). 실 PII는 게이트 안에 절대 들이지 않는다.
- 실 메일함 접근 전 선결: **owner/조직 동의**(어느 메일함을·누가 읽나), **스코프 최소화**(질문 채널로 지정된 주소/라벨만·전체 메일함 스윕 금지), **보존/마스킹 정책**(원문 보관 여부·PII 마스킹). 이 셋이 확정되기 전엔 A4/A5를 켜지 않는다(`ImapMailChannel`은 `NotImplementedError` 자리로 남는다).
- 실 발신(A5) 전 **노출 검수**: A2 투영이 게이트 내에서 노출 불변식을 강제하지만, 실 발신은 사람 검수 지점을 한 번 거친다(발신은 되돌릴 수 없으므로).

### 결정 4 — 노출 불변식은 메일 회신에도 그대로. 새 노출 표면(제목)까지 덮는다

메일 회신은 A2(`project_reply_to_mail`)의 투영만 나간다:

- **본문**은 `reply_to_mcp_text`(ADR 0006 노출 규율)를 재사용한다 — OrgReply(Answered | Pending)에서만 투영하므로 조직 내부값(confidence·candidates·manager_id·ticket_id·reason·intent·escalated_to)이 구조적으로 안 샌다.
- **제목**은 메일이 새로 여는 유일한 노출 표면이라, 내부 상태에 분기하지 않는다 — 원 제목 에코(`Re: …`·사용자 자신의 입력) 또는 중립 고정 문자열만. Answered/Pending·kind에 따라 제목을 바꾸지 않는다(coarse 상태조차 새 표면으로 새지 않게).

## 자체점검 — 4대 불변식

- **미아 없음.** 메일 질문도 0매칭이면 `AskOrg.handle`이 Pending(unowned)→Manager escalation으로 종착하고(무변경), A2가 중립 안내를 회신한다. 신원 매핑 실패도 질문을 버리지 않고 guest로 흘려보낸다(결정 2) — 채널 어느 지점에서도 질문이 드롭되지 않는다.
- **무효 카드 거부.** 이 ADR은 카드/admission을 건드리지 않는다(메일은 질문 채널이지 카드 등록 경로가 아님). `User`는 카드가 아니라 admission 무관(ADR 0021과 동일).
- **Authority 중앙.** 핵심 방어선이다 — 발신자 주소로는 담당권/권한이 서지 않는다(결정 1). Authority는 `routing_rules.yaml` 중앙 선언만이고(ADR 0004), 메일 채널은 답 전달일 뿐 권한 선언이 아니다. 스푸핑 발신자가 "나는 X다"라고 해도 그 주장은 라우팅/Authority에 반영되지 않는다.
- **전이 ≠ 기록.** 메일 송수신 로그(`FakeMailChannel.sent`)는 전달 사실의 운영 신호로 audit(절차 기록)과 별 축이다(ADR 0022 통지 전달 로그가 audit과 별 축인 것과 동형). 채널은 전이를 만들지 않는다(라우팅 전이는 `AskOrg.handle`·전이 도메인 소관).

## 범위 밖(후속·게이트 밖)

- 실 메일 수신 어댑터(IMAP/Graph poll·webhook·A4)·실 발신(SMTP/Graph·A5).
- 실 발신자 검증 메커니즘(SPF/DKIM/DMARC·조직 도메인 allowlist·SSO email 연계)의 구체 구현.
- 다주제 메일 분해·되묻기(A1 `AmbiguousMail`을 자동 처리하는 후속 정밀 파싱).
- PII 보존/마스킹 정책의 구체 규칙(결정 3의 선결 조건을 owner와 확정 후).
- 메일을 검증 코퍼스로 쓰는 B축(별도 ADR 0041 후보·이 ADR과 독립).

## 미결(사용자 결정 필요)

1. **메일 시스템·수신 방식** — 회사 메일(Gmail/Graph/IMAP)·poll vs webhook(A4 착수 전).
2. **신원 매핑 활성화 여부** — 결정 2의 검증 경유 매핑을 실제로 켤지, 아니면 메일은 전부 guest로만 둘지(기본은 guest·매핑은 옵트인).
3. **실 메일 접근 스코프·owner 동의·보존/마스킹 정책**(결정 3 선결).
