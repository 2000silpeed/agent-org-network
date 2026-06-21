# owner 지식 구성 공통 기준은 OKF(Open Knowledge Format) — owner 환경의 마크다운+프론트매터 번들을 워커가 cwd로 소비

상태: proposed (2026-06-21) · ADR 0010("답변 주체 = owner Claude Code", `knowledge_sources`는 출처 레이블·RAG 후속)의 *지식 소비 방식* 보강 · ADR 0006(중앙 무지식)·0004(카드 자기보고 보수성)·0012(DelegationSnapshot·staleness)와 정합 · 이번은 **ADR + 모델 shape + 문서**만(실 구현 = ClaudeCodeRuntime의 OKF cwd 소비는 후속 mcp-runtime-engineer 슬라이스)

## 맥락

PRD §3은 "구성원이 자기 업무 에이전트를 만들어 **자기 지식으로 답**한다"를 핵심 원칙으로 든다. ADR 0010은 답변 주체를 owner의 Claude Code로 못박았지만, *owner의 지식을 무엇으로 어떻게 구성·소비하는가*는 비워뒀다 — `knowledge_sources`는 "출처 *레이블*뿐(진짜 문서 RAG 아님)"이고, 실 문서 RAG(인덱싱·검색)는 "별도 후속 결정·이 ADR 범위 밖"으로 미뤘다. 그래서 현재 워커는 카드의 `summary`·`domains`만 프롬프트 컨텍스트로 받고, owner의 *실제 사적 지식*에는 닿지 못한다(ADR 0010 Consequences "owner의 실제 사적 지식에 접근하지는 못한다").

이 공백을 메우는 통상적 해법은 **벡터DB + RAG 파이프라인**(임베딩·청킹·검색기·인덱스 운영)인데, 이는 무거운 인프라 축이라 ADR 0010이 "큰 작업"으로 미뤘다.

**확정 사실(사용자 결정 + PoC 입증).** owner 지식 구성 공통 기준으로 Google Cloud의 **OKF(Open Knowledge Format)** 를 채택한다 — 마크다운 + YAML 프론트매터 파일 번들, 프론트매터의 `type`만 필수(값은 자유), 크로스링크로 엮인 문서 그래프, git/파일 기반, 인간과 AI가 함께 읽는 이중 소비, "에이전트가 README처럼 직접 읽는다"는 철학. 그리고 이 형식이 우리 구조에 *왜* 맞는지가 PoC로 입증됐다:

> `claude -p`를 OKF 번들 디렉터리를 cwd로 두고 `--allowedTools "Read,Glob,Grep"`로 돌리니, claude가 OKF 마크다운을 **스스로 읽어** 지식 기반의 구체적 답을 생성했다. 같은 질문을 빈 cwd에서 돌리면 "문서가 없어 판단 불가"로 공허했다.

즉 **벡터DB·RAG 파이프라인이 0이다.** Claude Code는 이미 *파일을 읽는 에이전트*(Read/Glob/Grep)라, owner 지식을 OKF 번들로 두고 그 디렉터리를 워커의 cwd로 주입하기만 하면 owner 지식 소비가 성립한다. ADR 0010이 무겁다고 미룬 RAG가 이 조합(cwd 주입 + 파일 읽기 도구)으로 싸게 풀린다 — 우리가 검색기를 짤 필요가 없고, claude가 cwd를 탐색한다.

이 ADR이 닫을 결정: (1) owner 지식 구성 = OKF 형식, (2) 카드와 번들의 분리(라우팅 메타 vs 답변 지식), (3) 워커의 OKF cwd 소비, (4) ADR 0010 `knowledge_sources` 보강, (5) ADR 0012 위임과의 연결, (6) 경계/연결점. **실 구현(ClaudeCodeRuntime의 cwd 소비·샘플 번들·동기화)은 이 ADR 범위 밖** — shape와 체크리스트만 둔다. type 어휘는 **강제하지 않는다**(아래 결정 1).

## 결정

### 1. owner 지식 구성 공통 기준 = OKF 형식, 단 우리는 type 어휘를 강제하지 않는다

owner가 자기 지식을 구성하는 공통 기준을 **OKF**로 둔다 — 한 owner의 지식은 **마크다운 + YAML 프론트매터 파일의 번들(Knowledge Bundle, "OKF 번들")** 이다. 형식 규칙은 OKF 최소주의를 그대로 가져온다:

- 각 문서는 마크다운 본문 + YAML 프론트매터. **프론트매터의 `type`만 필수**, 값은 자유.
- 문서끼리 크로스링크로 엮이며(`index.md` 같은 진입 문서를 권장하되 강제 아님), 전체가 하나의 지식 그래프.
- git/파일 기반 — owner 환경(개인 PC 또는 owner별 격리 저장소)에 git repo로 둔다.
- 인간과 AI가 *같은 파일을* 읽는다(이중 소비). 별도 인덱스·임베딩 산출물이 없다.

**우리는 `type` 어휘를 강제하지 않는다.** Agent Org Network는 범용 플랫폼이므로, `type`이 `runbook`이든 `policy`이든 `faq`이든 owner·조직이 자유롭게 정의한다 — OKF 최소주의(필수는 `type` 키의 존재뿐, 값은 owner 도메인)를 그대로 따른다. 우리가 어휘 표준을 박으면 범용성이 깨지고 OKF의 가벼움을 잃는다. (조직이 자기 컨벤션을 두는 건 owner 자유 — 플랫폼이 강제하지 않을 뿐.)

근거:
- **PRD §3 "자기 지식으로 답"의 실체화** — 레이블(`knowledge_sources`)이 가리키던 *추상적 출처*를 실제로 읽을 수 있는 *구체적 지식 본체*(OKF 번들)로 바꾼다. 이는 PRD와의 충돌이 아니라 *빈자리 채움*이다.
- **RAG 인프라 회피** — Claude Code가 파일 읽는 에이전트라 검색기·임베딩·벡터DB가 불요(PoC 입증). ADR 0010이 무겁다고 미룬 걸 가벼운 조합으로 푼다.
- **이중 소비** — owner가 자기 지식을 사람용으로 쓴 마크다운을 AI(워커)가 그대로 읽는다. 별도 "AI용 변환" 단계가 없어 owner 부담이 작다.

### 2. 카드/번들 분리 (안 B) — `AgentCard`는 라우팅 메타(중앙), OKF 번들은 답변 지식(owner 환경)

`AgentCard`와 OKF 번들은 **분리된 두 객체**다.

- **`AgentCard`** = Registry에 등록되는 *라우팅 메타*. `domains`·`can_answer`·`cannot_answer` 등으로 **라우터가 후보를 판정하고 admission 불변식을 강제**하는 단위(ADR 0004·0005 그대로). 중앙이 보는 것.
- **OKF 번들** = owner 환경에 있는 *답변 지식 본체*. 워커가 답을 만들 때 읽는 것. 중앙은 본체를 보지 않는다.
- **`knowledge_sources`** = 카드 안에서 그 owner 환경의 **OKF 번들을 가리키는 참조(레이블 → 번들)**. 카드는 "라우팅상 내가 무엇을 담당하나"를 들고, 번들 참조로 "그 답을 어디서 끌어오나"를 가리킨다.

**카드를 OKF 번들에 흡수하지 않는다(흡수 안 = 기각).** 카드를 OKF 문서 중 하나로 녹이면 admission 불변식(유효 카드만 등록)·Authority 중앙(카드 자기보고 보수성)이 흔들린다 — 라우팅 메타는 중앙 Registry가 검증·보관하는 경계여야 하고, owner 환경의 자유로운 마크다운 번들과 같은 곳에 둘 수 없다. 카드는 *중앙이 보는 라우팅*, 번들은 *owner 환경의 답변 지식* — 책임과 위치가 다르다.

**중앙 무지식 보존(ADR 0006).** 중앙은 카드 메타(라우팅용)만 보고, OKF 본체는 owner 환경에 있다. 중앙이 owner 문서를 모아 보지 않는다 — 워커가 owner 환경(cwd)에서 읽을 뿐이다. OKF 채택은 중앙 무지식을 *깨지 않고*, 오히려 "지식은 owner 환경에, 라우팅 메타만 중앙에"라는 경계를 더 또렷하게 만든다.

### 3. 워커의 OKF 소비 = `ClaudeCodeRuntime`이 owner OKF 번들을 cwd로 두고 `claude -p`를 Read/Glob/Grep 도구와 함께 실행 (PoC 검증)

답을 만드는 워커(`ClaudeCodeRuntime`, ADR 0010 T6.1)가 OKF 번들을 *소비*하는 방식:

- 현재 `_run_claude_headless`는 응답 잡음·프로젝트 CLAUDE.md 간섭을 막으려 **임시 디렉터리(`tempfile.TemporaryDirectory`)를 cwd로** 쓴다(빈 cwd라 지식 없음). 이를 **owner OKF 번들 디렉터리를 cwd로** 바꾼다.
- `claude -p`에 `--allowedTools "Read,Glob,Grep"`를 더해 claude가 cwd의 OKF 마크다운을 *읽도록* 허용한다(현재는 도구 없이 텍스트 답만).
- 프롬프트(`_build_persona_prompt`)에 "cwd의 OKF 지식을 읽고 그 근거로 답하라"는 지시를 더한다 — claude가 번들을 탐색·인용하게.

이는 ADR 0010의 T6.1→T6.3 분산과 정합한다: T6.3에서 답변 주체가 owner 환경이 되면, 그 owner 환경에 OKF 번들이 있고 워커가 그 디렉터리를 cwd로 읽는다 — "owner별 지식 격리"가 *번들 cwd 격리*로 실체화된다(각 워커는 자기 owner 번들만 cwd로 본다).

**결정론 보존(ADR 0003).** OKF 소비도 `claude -p` 호출이라 비결정·느리다. 단위 테스트는 `runner` 주입(FakeRunner)으로 고정하고, 실 OKF 소비 품질은 eval/수동 시연으로 본다(ADR 0010 그대로). cwd가 임시→번들로 바뀌어도 이 경계는 그대로다.

### 4. ADR 0010 `knowledge_sources` 보강 — "출처 레이블·RAG 후속" → "OKF 번들 참조, 워커가 cwd로 소비(RAG 인프라 불요)"

ADR 0010 Consequences는 "`knowledge_sources`는 아직 출처 레이블 … 진짜 문서 RAG(인덱싱·검색)는 별도 후속 결정"이라 적었다. 이 ADR이 그 후속 결정을 닫는다 — **0010을 직접 수정하지 않고, 0013이 보강·참조**한다(ADR 0012가 0010을 직접 고치지 않고 보강한 것과 같은 방식):

- `knowledge_sources`는 더 이상 "이름뿐인 출처 레이블"이 아니라 **owner 환경의 OKF 번들을 가리키는 참조**다(결정 2). 워커가 cwd로 소비한다.
- "진짜 문서 RAG"는 **벡터DB·검색기 인프라 없이** 풀린다 — Claude Code가 파일 읽는 에이전트라(Read/Glob/Grep) cwd 주입만으로 owner 지식 소비가 성립한다(PoC). 0010이 "큰 작업"으로 미룬 RAG의 *가벼운 실현*이다.
- ADR 0010의 다른 결정(답변 주체 = owner Claude Code, 중앙 무지식, API 키·비용 회피)은 **전부 불변**. 이 ADR은 0010의 *지식 소비 방식*만 구체화한다.

### 5. ADR 0012 위임과의 연결 — owner가 백업에 위임하는 것이 곧 OKF 번들, `DelegationSnapshot`이 그 메타

ADR 0012 결정 3은 "owner가 백업에 위임하는 것 = `knowledge_sources`와, 장래의 실 지식 인덱스(0010이 후속으로 둔 RAG)의 스냅샷"이라 했다. 이 ADR이 "장래의 실 지식 인덱스"의 정체를 확정한다 — **그게 OKF 번들이다.**

- owner가 백업 워커에 위임하는 격리 스냅샷의 *본체*는 **OKF 번들의 스냅샷**이다(문서·인덱스가 아니라 마크다운 번들).
- `DelegationSnapshot`(`owner_id`·`agent_ids`·`snapshot_at`)은 그 **OKF 번들 스냅샷의 메타**다 — `snapshot_at`은 *번들을 뜬 시각(번들 신선도)*이고, staleness 거부(ADR 0012 결정 9)는 "OKF 번들이 너무 오래되면 백업이 그 번들로 답하지 않고 escalation"으로 읽힌다.
- 이는 0012의 shape를 *바꾸지 않는다* — `DelegationSnapshot`의 필드·staleness 정책은 그대로고, 이 ADR은 그 메타가 가리키는 *본체가 OKF 번들*임을 명시할 뿐이다. "실 데이터 본체는 owner별 격리 저장소에 있고 백업 인스턴스만 owner 키로 접근"(0012 결정 3·5)도 그대로 — 그 "실 데이터 본체"가 OKF 번들이다. 중앙·디스패처는 여전히 메타만 본다(중앙 무지식).

### 6. 경계 / 연결점 (실 구현 후속)

OKF cwd 소비가 여는 운영 축은 **연결점·주의로만** 두고 실 구현은 후속(mcp-runtime-engineer)이다:

- **파일 접근 격리** — cwd가 OKF 번들로 한정돼야 한다(민감 파일·다른 번들·홈 디렉터리 노출 금지). MVP는 "cwd = OKF 번들 디렉터리 한정, 민감 파일은 번들에 안 둠"을 **owner 책임**으로 두고, 도구 허용도 `Read,Glob,Grep`(읽기 전용)으로 좁힌다. 실 샌드박싱·권한 강제는 후속.
- **응답 지연** — claude가 도구 호출(파일 탐색)을 하므로 텍스트-만-답보다 느릴 수 있다. timeout 예산(ADR 0011·0012의 t1/t2)이 이를 흡수하는 자리이며, 실 튜닝은 후속.
- **번들 신선도** — OKF 번들이 owner 실 지식과 동기화돼야 한다(특히 백업 위임 시). 동기화 트리거·staleness는 ADR 0012 결정 9(owner 수동+주기+변경 이벤트, 임계 초과 시 거부)가 이미 자리를 잡았다 — OKF 번들에 그대로 적용된다.
- **실 동기화·암호화·키 관리** — ADR 0012 결정 5의 연결점 그대로(후속). OKF 번들이 "실 데이터 본체"라는 점만 더해질 뿐, 동기화·암호화 정책은 0012가 이미 후속으로 둔 자리다.

## Considered Options

### owner 지식 구성 형식
- **OKF(선택)** — 마크다운+프론트매터 번들, git/파일 기반, Claude Code가 직접 읽음. PoC로 cwd 소비 입증. RAG 인프라 0.
- **벡터DB + RAG 파이프라인(기각)** — 임베딩·청킹·검색기·인덱스 운영이라는 무거운 인프라 축. ADR 0010이 "큰 작업"으로 미룬 그것 — Claude Code가 파일을 직접 읽을 수 있어 불필요해졌다.
- **구조화 DB/스키마(기각)** — owner가 자기 지식을 정형 스키마로 넣어야 해 부담이 크고, "사람과 AI가 같은 문서를 읽는다"는 이중 소비를 잃는다.

### type 어휘
- **강제하지 않음(선택)** — owner/조직 자유 정의. 범용 플랫폼·OKF 최소주의 정합.
- **표준 어휘 강제(기각)** — 우리가 `runbook`/`policy` 같은 타입 집합을 박으면 범용성이 깨지고 OKF의 가벼움을 잃는다. (조직이 자기 컨벤션을 두는 건 owner 자유.)

### 카드와 OKF 번들의 관계
- **분리(안 B, 선택)** — 카드=라우팅 메타(중앙 Registry), 번들=답변 지식(owner 환경), `knowledge_sources`가 카드→번들 참조. 중앙 무지식·admission·Authority 중앙 전부 보존.
- **카드를 OKF에 흡수(안 A, 기각)** — 카드를 OKF 문서 중 하나로 녹이면 admission 불변식·Authority 중앙이 흔들린다(라우팅 메타가 owner 환경의 자유 마크다운으로 새어나감). 라우팅 메타는 중앙이 검증·보관하는 경계여야 한다.

### `knowledge_sources` 모델 변경 범위
- **의미만 재정의, 필드 유지(선택)** — `knowledge_sources: list[str]`를 그대로 두고 "출처 레이블" → "OKF 번들 참조"로 *의미를 재정의*한다. 카드 스키마·하위호환 불변(기존 카드·테스트가 깨지지 않음), 자기보고 보수성 정합(under-claim — "내 지식은 여기 있다"는 출처 표시이지 권한 주장이 아님).
- **새 필드 `okf_bundle` 추가(기각, 지금은)** — 별 필드를 더하면 "출처 레이블"과 "번들 경로"가 두 축으로 흩어지고, 기존 카드 마이그레이션이 필요하다. 지금 필요한 건 *의미 확정*이지 스키마 확장이 아니다. (장래에 번들 경로의 *구조화된 참조*(경로+리비전 등)가 필요해지면 그때 전용 필드를 검토 — 이 ADR은 최소 변경.)

### OKF 채택 범위
- **형식만 채택 + cwd 소비(선택)** — 형식(마크다운+프론트매터 번들)과 소비 방식(cwd+도구)만 정하고, type 어휘·동기화 파이프라인·샌드박싱은 owner 책임/후속으로 둔다. 지금 필요한 만큼만.
- **OKF 전면 도입(기각)** — type 표준·인덱싱·동기화 인프라까지 한 번에 닫는 건 과하다. PoC가 입증한 건 "cwd 소비가 된다"이지 "전체 OKF 거버넌스가 필요하다"가 아니다.

## Consequences

- **PRD §3 "자기 지식으로 답"이 실체화된다** — `knowledge_sources`가 가리키던 레이블이 실제 읽히는 OKF 번들이 된다. 충돌이 아니라 빈자리 채움이며, "중앙 무지식"과 어긋나지 않는다(번들은 owner 환경, 중앙은 카드 메타만).
- **RAG 인프라가 불요해진다** — ADR 0010이 미룬 "진짜 문서 RAG"가 cwd 주입 + Read/Glob/Grep으로 풀린다. 벡터DB·임베딩·검색기 운영을 떠안지 않는다.
- **`AgentCard` 스키마 무변경** — `knowledge_sources` 필드는 그대로, 의미만 "OKF 번들 참조"로 재정의(결정 2·모델 변경 범위). 기존 카드·테스트·admission 불변. `_build_persona_prompt`의 "근거로 삼을 출처(레이블)" 문구는 OKF 소비 구현 시 "cwd OKF 읽고 근거로 답"으로 정밀화될 자리(후속 mcp-runtime-engineer).
- **`ClaudeCodeRuntime`의 cwd가 임시→OKF 번들로 바뀐다(후속 구현)** — `_run_claude_headless`의 `tempfile.TemporaryDirectory()` cwd가 owner 번들 디렉터리로, `--allowedTools "Read,Glob,Grep"` 추가, 프롬프트에 OKF 소비 지시. `runner` 주입 경계(결정론 테스트)는 보존.
- **ADR 0010 보강(직접 수정 안 함)** — `knowledge_sources`·RAG 후속 항목이 이 ADR로 닫힌다(결정 4). 0010의 답변 주체·중앙 무지식·비용 회피는 불변.
- **ADR 0012 위임의 본체가 OKF 번들로 확정된다** — `DelegationSnapshot.snapshot_at`이 OKF 번들 신선도, staleness 거부가 "오래된 번들로 안 답함"으로 읽힌다. 0012 shape 무변경.
- **새 도메인 용어** — CONTEXT에 **OKF / Knowledge Bundle(OKF 번들)** 을 더하고(_Avoid_ 포함), Agent Runtime·`knowledge_sources` 절을 "cwd OKF 소비"로 갱신. 맨 "Agent" 단독 금지 그대로.
- **연결점만(실 구현 후속)** — 파일 접근 격리(cwd 한정·민감 파일 제외, owner 책임)·응답 지연(도구 호출)·번들 신선도(0012 staleness)·실 동기화·암호화·키(0012 결정 5 후속). 이번은 ADR·shape·문서뿐.

### 구현 분담(후속, mcp-runtime-engineer)

- `ClaudeCodeRuntime` OKF cwd 소비 — `_run_claude_headless`에 cwd 인자(owner 번들 경로)·`--allowedTools "Read,Glob,Grep"`·프롬프트 OKF 지시. `runner` 주입 결정론 보존.
- 샘플 OKF 번들 — 데모용 owner 1~2명의 마크다운+프론트매터 번들(`index.md` + 문서 몇 개, type 자유) + cwd 와이어링(`demo.py`/`server.py`).
- 결정론 테스트 — FakeRunner로 "cwd가 owner 번들로 설정됐다 / 도구 플래그가 붙었다 / 프롬프트에 OKF 지시가 있다"를 고정 검증(실 claude·실 파일 탐색은 수동/eval).
- demo 와이어링 — owner별 번들 디렉터리를 워커가 cwd로 잡도록(T6.3 분산·T6.6 백업 cwd 정합). T6.7 체크리스트.
