# Routing

조직 구성원의 업무 에이전트 카드를 등록받아, 들어온 질문을 책임 있는 담당자에게 연결(라우팅)하는 컨텍스트. 중앙은 지식을 소유하지 않고 "누가 무엇을 담당하는가"와 그 연결·기록만 관리한다.

## Language

### Graph model

두 종류의 노드와 그 사이의 엣지로 본다. 역할(Owner/Manager/Maintainer)은 노드가 아니라 엣지에서 파생된다.

- **노드**: User(사람), Agent Card(업무 에이전트)
- **엣지**: `owns`(User→Agent, 그 User가 Owner) · `manages`(User→User, 윗 User가 Manager) · `maintains`(User→Agent, 그 User가 Maintainer)
- **카디널리티**: User의 manager는 0..1(루트는 0) · 한 User는 여러 Agent를 owns · 한 Agent의 Owner는 정확히 1명

### Core

**Agent Card**:
한 책임(역할/직무)의 담당 영역·답변 가능 범위·이관 규칙을 선언한 YAML 문서. 정체성(`agent_id`)은 역할에 묶여 담당자가 바뀌어도 불변. 레지스트리에 등록되는 단위이자 라우터가 평가하는 후보 단위 — 등록과 라우팅 양쪽에서 동일한 객체다. `agent_id`는 **wire-format admission 규칙**(`^[A-Za-z0-9][A-Za-z0-9_-]*\Z`·≤64자 — 영숫자로 시작 + 영숫자/`_`/`-`)을 카드 구성 시점(`AgentCard` field validator)에 *근본 강제*한다(ADR 0023): 경로 탈출(`/`·`\`·`..`·절대·빈·공백·후행 개행)을 *구조적으로* 차단해 "유효하지 않은 카드는 등록되지 않는다"의 일부를 이룬다(형식 위반은 빌더 `{ok:False}`·로더 `RegistryError`로 매핑). 형식 강제는 *식별자 위생*이지 권한 선언이 아니다(Authority 중앙 무관, ADR 0004). 대문자 수용(소문자 강제는 명명 스타일이라 기각·`agent_X`/`agent_Y` 픽스처 보존). admission=형식 권위, 어댑터(`validate_agent_id`)=경로 안전 백스톱으로 역할 분리(심층 방어).
_Avoid_: Agent(단독), Persona, Profile

**Owner**:
한 Agent를 `owns` 하는 User — 그 에이전트의 **관리 주체(거버넌스)**이고 그 카드의 답변에 책임진다. **owner의 힘은 런타임을 호스팅해서가 아니라 *정의·지식·범위·승인·검토·소유·라이프사이클을 쥐고 있어서* 나온다(ADR 0017)** — 정의(카드 편집)·지식 유지(OKF 소유)·범위 통제(거부·승인)·답 검토·정정(`BackupReview`)·충돌 시 담당 결정(1인칭 합의)·신원 책임 귀속(`answered_by`)·라이프사이클(Transfer). 이 일곱 거버넌스 능력은 전부 *중앙 실행과 양립*하며 대부분 이미 구현됐다 — owner가 *실행 주체*일 필요는 없다(중앙이 owner 통제 지식으로 실행해도 owner 주권은 100% 유지). 카드의 *정체성*이 아니라 *교체 가능한 관계(엣지)* — 담당자가 바뀌면 owns만 갈아끼운다. owner 종속(binding)의 *증명*은 SSO 신원(미싱피스 — 지금 무비밀번호라 명목뿐, ADR 0017 결정 5·Phase 7 T7.1).
_Avoid_: Author

**Maintainer**:
한 Agent를 `maintains` 하는 User — 카드 편집 권한자. MVP에선 Owner와 동일(maintains 엣지 없으면 owns가 대신). 편집 권한·이력은 git/PR이 강제·기록한다.
_Avoid_: Editor, Admin

**User**:
사람 노드. Agent를 owns 하거나 다른 User를 manages 할 수 있다. `id`와 상위 `manager`(0..1)를 가진다.
_Avoid_: Account, Member

**Agent Runtime**:
Agent Card를 구동해 질문에 답하는 실행 주체. 라우터가 호출한다. **관리/실행 분리(ADR 0017)** — 답의 *지식 출처·책임*은 owner(거버넌스 주체)이되 *실행*은 **중앙 `claude -p`가 owner 통제 지식(OKF)의 *최신 커밋 스냅샷*을 cwd로 읽어** 만든다. "자기 지식으로 답"은 *실행 위치*가 owner라서가 아니라 *지식 출처가 owner*(owner 소유·유지)라서 성립하고, 답은 owner에 귀속돼 owner가 검토한다. 중앙은 owner의 **대행자(deputy)**이지 대체자가 아니다 — 카드 정의를 못 바꾸고, 선언 범위를 못 넘고, 승인을 못 우회하고, 소유를 못 가져간다. 중앙 API 키 LLM RAG는 *여전히 안 쓴다*(중앙은 지식의 *소유자·진실 원천이 아니라* 답변 시 최신을 *읽을* 뿐 — RAG 인덱스로 들지 않는다, ADR 0010 유효 근거 보존). 포트로 분리 — `StubRuntime`(canned, 테스트·스켈레톤) → `ClaudeCodeRuntime`(`claude -p` 헤드리스, **중앙 실행·owner OKF 최신 읽기 = 기본 경로**). *분산 owner-실행 경로(owner 워커·MCP/A2A)는 기본에서 사설 데이터 커넥터 옵션으로 강등됨*(아래 Distributed transport 절·ADR 0017 — 답이 owner의 사설·실시간 데이터에 의존해 중앙이 가질 수 없을 때만). **owner 지식 소비 = OKF 번들 cwd 소비(T6.7 구현, ADR 0013)**: `ClaudeCodeRuntime`이 owner의 **OKF 번들 디렉터리를 cwd로** 두고 `claude -p`를 `--allowedTools "Read,Glob,Grep"`와 함께 돌려 claude가 번들을 *읽어* 답한다(프롬프트에 "cwd OKF 읽고 근거로 답"). 벡터DB·RAG 인프라 0 — Claude Code가 파일 읽는 에이전트라 cwd 주입+읽기 도구면 owner 지식 소비가 성립(PoC 입증). 답변 일관성은 *커밋 스냅샷*(읽기전용)으로 — "이 답은 이 커밋 기준" 감사까지(ADR 0017 결정 3). `knowledge_sources`는 그 OKF 번들을 가리키는 참조이며(아래 절 — "출처 레이블뿐"에서 의미 재정의), `Answer.sources`로도 흐른다. (ADR 0007·0010·0013·0017)
_Avoid_: Agent(단독), Bot

**OKF (Open Knowledge Format)**:
owner가 자기 지식을 구성하는 공통 기준(ADR 0013) — Google Cloud의 OKF를 채택. **마크다운 + YAML 프론트매터 파일의 번들**, 프론트매터의 `type`만 필수(값 자유 — 우리는 type 어휘를 강제하지 않는다, 범용 플랫폼·OKF 최소주의), 크로스링크로 엮인 문서 그래프(`index.md` 진입 권장·강제 아님), git/파일 기반. 인간과 AI가 *같은 파일*을 읽는 이중 소비 — 중앙 `claude -p`가 cwd로 두고 README처럼 직접 읽는다(Read/Glob/Grep). 벡터DB·RAG·임베딩 인프라 없이 owner 지식 소비가 성립(PoC 입증). PRD §3 "자기 지식으로 답"의 실체. **살아있는 지식 = git 저장 + 빌더 UI 편집 + 신선도 1급(ADR 0017 결정 3·Phase 7 T7.2·T7.3)**: OKF를 git에 둬(owner별 repo 또는 모노repo+CODEOWNERS) 버전·이력·owner 범위 쓰기·PR 리뷰·변경 웹훅을 *공짜로* 얻고, 비개발자 owner를 위해 **빌더 UI가 owner 대신 커밋**한다(owner는 git을 몰라도 됨). 실행은 *커밋 스냅샷*(읽기전용)을 cwd로 — agent별 디렉터리면 충분(워크트리 불필요). **신선도·변경 전파가 "살아있음"의 심장**: 지식 변경의 *미래 답* 전파는 쉽고(최신 읽기), *과거 판례·답* 전파가 어렵고 핵심이다 — 정책이 바뀌면 그 정책에 기댄 Precedent·답을 자동 재검토/무효화하고 `last_reviewed_at`를 신뢰 신호로 노출(stale 지식은 owner에 갱신 nudge).
_Avoid_: RAG(이건 인프라 — OKF는 형식·파일 직독으로 RAG 불요), Vector store, Index(단독), Schema

**Knowledge Bundle (OKF 번들)**:
한 owner의 OKF 문서 묶음 — **git에 보관하는** owner 통제 답변 지식 본체(owner가 *소유·유지*, 편집 채널은 git/PR·빌더 UI). 중앙 `claude -p`가 답을 만들 때 **그 특정 커밋 스냅샷을 cwd로 읽는** 디렉터리다(ADR 0017 — 실행은 중앙이되 지식 출처·소유는 owner). owner의 힘은 이 번들을 *쥠*에서 나온다(중앙은 최신을 읽을 뿐 소유·진실 원천이 아님). **AgentCard와 분리**(ADR 0013 안 B): 카드 = Registry에 등록되는 *라우팅 메타*(domains·can_answer — 라우터 후보 판정·admission), 번들 = owner 통제 *답변 지식*. 중앙은 카드 메타를 보고 번들의 *최신을 읽되* RAG 인덱스로 들지 않는다(중앙 무지식 재해석 — 소유·진실 원천이 아님). `knowledge_sources`가 카드→번들 참조. 카드를 번들에 흡수하지 않는다(admission·Authority 중앙 흔들림 — 기각). `last_reviewed_at`·`snapshot_at`이 번들 신선도(ADR 0017 결정 3 — 신선도 1급, stale 지식 갱신 nudge·과거 판례 재검토). *분산/위임 경로의 본체로도 쓰임* — owner가 사설 데이터 커넥터(옵션 B)로 가거나 백업에 위임할 때 그 본체가 이 번들 스냅샷이며(`DelegationSnapshot.snapshot_at`이 그 staleness 기준), 이는 *기본 경로(중앙 최신 읽기)와 양립하는 옵션*이다(ADR 0017 결정 4). **저장·편집·스냅샷 실행(ADR 0018 — T7.2)**: 번들은 *모노repo 하위폴더 `okf/{agent_id}/`*(MVP, owner별 repo는 후속 옵션)에 git으로 저장되고, **빌더 UI(OKF Builder 면)가 owner 대신 커밋**한다(owner는 git 몰라도 됨·author=owner 신원·버전/리뷰/감사는 git). 빌더가 자동 커밋하는 본체는 *OKF 번들 마크다운*이지 **카드 YAML이 아니다**(카드는 admission 경계라 검증→YAML→PR 유지·ADR 0013 안 B). 답 실행은 *커밋 스냅샷*(`git archive <sha>` 추출 cwd·working tree 직독 아님)을 읽어 "이 답은 이 커밋 기준"을 재현(`Answer.snapshot_sha`). 커밋·스냅샷은 `GitGateway` 포트로 추상(아래 용어).
_Avoid_: Agent Card(라우팅 메타와 혼동 — 카드와 별 객체), Knowledge base(모호), RAG corpus, Document store(단독)

**Answer**:
Agent Runtime이 한 질문에 산출한 답. `text`(답 본문) · `sources`(근거 출처) · `mode` · `snapshot_sha`(선택)를 가진다. `mode`는 신뢰 상태 — `full`(owner 실시간 답, 그대로 사용자에게) · `draft_only`(Approval 게이트가 걸려 사람 승인 전까지는 초안) · `backup`(owner 위임 백업 워커의 스냅샷 기반 답, owner 미검토 — 신뢰 하향, ADR 0012). **`snapshot_sha`**(ADR 0018 결정 4)는 *이 답이 어느 OKF 커밋 스냅샷으로 만들어졌나* — 커밋 스냅샷 실행(`git archive <sha>` 추출본을 cwd로 읽음)에서 그 SHA가 실려 "이 답은 이 커밋 기준" 감사가 된다(기본 `None` — working tree 직독·canned/스텁 경로엔 SHA 없음·하위호환). `mode`·`sources`와 같은 *답에 붙는 신뢰/출처 메타*의 연장으로, 운영 면 노출 OK(채팅 노출은 `sources` 정신으로 후속 판단). 즉 Routed에 Approval이 붙으면 Runtime은 `draft_only`로 답하고(승인 ≠ 라우팅, 게이트만 걸림), owner PC 부재로 백업이 답하면 `backup`으로 신뢰가 하향된다(둘이 겹치면 `backup`이 우선 — 백업은 어차피 owner 미검토라 더 강한 하향). **`backup`은 셋의 마지막 값이고 넷째 값은 없다** — staleness로 fresh/stale을 쪼개지 않는다(stale 백업은 답을 안 하고 거부→escalation이라 *답이 나가는* 백업은 다 fresh, ADR 0012 결정 9). owner가 복귀해 백업 답을 검토(`BackupReview`)하면 backup→`full`로 *신뢰 복원*(승인·정정 모두 — owner 검토를 거쳤으므로, 재노출 시 적용, ADR 0012 결정 7). "답에는 항상 담당·신뢰 상태(승인/초안/백업/출처)가 붙는다"는 불변식의 그 답. `mode`는 *원래 노출하는 신뢰 상태값*이라 사용자向 `Answered`에 그대로 실린다(조직 내부 구조가 아님 — 노출 불변식과 양립).
_Avoid_: Response, Result, Reply

### Distributed transport (분산 전송 — 사설 데이터 커넥터 옵션 B로 재포지셔닝, ADR 0017)

> **재포지셔닝(ADR 0017)**: 이 절의 분산 전송(Owner Worker·Backup Worker·WebSocketDispatcher 등)은 *기본 경로*에서 **사설 데이터 커넥터 한정 옵션(옵션 B)**으로 강등됐다 — 답이 owner의 사설·실시간 데이터/도구(자기 DB·메일·사내 API·자격증명)에 의존해 *중앙이 그 데이터를 가질 수 없을* 때에 한해 그 담당이 *데이터 접근만* 노출하는 경로다("답 생성 전체를 노트북에 보내기"가 아님). 기본 답 실행은 중앙 `claude -p`가 owner OKF 최신을 읽어 만든다(Agent Runtime 절). 아래 용어·구현물은 **삭제하지 않고 보존** — (a) owner-실행이 *가능함*을 입증했고(옵션 B의 토대), (b) 디스패처·작업 큐·재연결·멱등 인프라는 ADR 0017 결정 6의 *비동기 처리·실시간 충돌 푸시 통지*(Phase 7 T7.4)로 재활용된다. **옵션 B 진입 판단은 ADR 0020**(세 질문): 답이 OKF 커밋 스냅샷만으로 grounding되면 기본 경로(중앙 실행) · owner 사설·실시간 데이터에 의존하면 **B-1 사설 데이터 커넥터**(데이터 접근만 노출·실행은 중앙) · 중앙이 읽는 것조차 정책상 금지면 **B-2 하드 데이터 격리**(owner 환경 실행 — 이때만 분산 강제, 아래 Owner Worker·Backup Worker가 그 실현). B-1과 B-2를 혼동 말 것(데이터가 사설이란 사실만으로 곧장 B-2 점프 금지 — B-1이 먼저 거른다). *기본 경로 라벨로 읽지 말 것 — 사설 데이터 옵션·푸시 인프라의 부품으로 읽을 것.*

owner PC는 서버를 노출하지 않는다(NAT/방화벽 뒤·고정 IP 없음·상시 가동 X). 그래서 owner PC의 워커가 중앙에 *역방향 아웃바운드*로 연결해 작업을 가져가고, 중앙은 owner별 작업 큐에 적재해 비동기로 답을 수집한다. 논리적 호출 방향(질문 중앙→owner)과 물리적 연결 방향(소켓은 owner→중앙)은 분리된다. (ADR 0011 — Agent Runtime의 도달 방식, 답변 주체=owner Claude Code는 ADR 0010 그대로. **ADR 0017로 옵션 B 재포지셔닝**.)

**Owner Worker (오너 워커)**:
*(기본 경로 아님 — 사설 데이터 커넥터 옵션 B의 부품, ADR 0017. 기본 답 실행은 중앙 `claude -p`가 owner OKF 최신 읽기.)* 각 Owner PC에서 도는 작은 실행 주체. 중앙에 *아웃바운드 WebSocket*으로 연결(ADR 0011 결정 6 — 폴링이 아니라 WS로 확정)해 자기 owner 작업 큐의 작업을 받아(중앙이 그 소켓으로 `PushWork`), 로컬 Claude Code(`ClaudeCodeRuntime` 재사용)로 답을 만들어 중앙에 회신(`SubmitAnswer`)한다. 중앙이 owner PC로 인바운드 연결을 *걸지 않는다* — 연결을 거는 쪽은 항상 워커다. owner PC는 간헐 연결이 정상이라 끊김·재연결·중복 전달이 핵심 실패 모드다(ADR 0011 결정 6-4). 진짜 그 owner인지는 인증으로 강제한다(ADR 0009 연결점, 실 검증 T6.5). 선례: Claude Code Remote Control(owner claude가 claude.ai에 아웃바운드 연결). 구현(`worker.py`, 슬라이스2b-ii): 프레임 핸들링 결정론 코어(`WorkerLogic`=`PushWork`→`ClaudeCodeRuntime`→`SubmitAnswer`·`backoff_seconds`·`parse_central_frame`, 단위 테스트)와 실 아웃바운드 WS·재연결 셸(`run_worker`, 수동 시연)을 분리. 자기 owner 카드(`agent_id→AgentCard`)는 owner 환경에 들고(`PushWork`는 식별자만 싣고 워커가 카드 복원), 한 owner = 한 워커 프로세스.
_Avoid_: Agent(단독), Client(단독 — 논리 호출 방향과 혼동), Node

**Backup Worker (백업 워커)**:
*(기본 가용성 경로 아님 — ADR 0017로 옵션 B 하위 케이스로 강등. 중앙 실행이 기본(24/7·최신 읽기)이라 owner PC 부재 폴백이 1급 가용성 문제가 아니게 됨. 단 백업의 **복귀 검토 루프(`BackupReview` — 승인·정정·무시)는 owner 거버넌스 능력 "답 검토·정정"으로 승격·보존**된다, ADR 0017.)* owner PC 워커가 부재(미연결/timeout)일 때 그 owner를 *대신 답하게* 하는 워커 — 단, 중앙 공용 LLM이 아니라 **owner가 명시적으로 위임한 자기 데이터·자기 신원의 격리 인스턴스**다(ADR 0012). 물리적으론 중앙 인프라에 호스팅되지만 논리적으론 *여전히 owner가 답한다* — 그래서 ADR 0010("답변 주체 = owner Claude Code")을 깨지 않고 "owner가 위임·관리하는 환경(owner PC **또는** owner 백업 인스턴스)"으로 보강한다. 새 타입이 아니라 **`WorkerLogic` 재사용** — `PushWork`→`ClaudeCodeRuntime`→`SubmitAnswer`의 같은 프레임 흐름을 타고, 다른 건 *어디서 도는가*(중앙 호스팅 격리)와 *무엇을 들고 도는가*(owner PC 실데이터가 아니라 위임 스냅샷=`DelegationSnapshot`)뿐(둘 다 생성자 인자로 흡수, `worker.py` 무변경). 답은 owner 미검토·스냅샷 기반이라 **신뢰 하향**(`Answer.mode="backup"`). 책임은 여전히 owner(opt-in 위임이므로 — `answered_by`=owner 불변). 폴백 사슬: owner PC 워커 → (부재) Backup Worker → (백업도 부재/실패) Manager escalation(미아 없음 — 반드시 종착). 진짜 그 owner가 위임한 백업인지는 인증으로 강제(ADR 0009 → T6.5).
_Avoid_: Fallback LLM(중앙 공용 추론과 혼동), Agent(단독), Standby(상시/기동 무관)

**WorkerRole (워커 등급)**:
한 owner에 붙은 워커의 우선순위 등급 — `primary`(owner PC 워커, 1차)와 `backup`(owner 위임 백업 워커, 2차). 같은 owner 안에서 *어느 연결로 먼저 push하나*를 가른다(신원=`owner_id`는 같고, 등급은 그 owner 안의 우선순위). `RegisterWorker`에 실려 워커가 자기 등급을 선언하고(PC=기본 `primary`, 백업=`backup`), `WebSocketDispatcher`의 연결 레지스트리가 owner당 등급별 연결(`owner_id → {role → send}`)로 보관해 push 시 우선순위 선택(primary 있으면 primary, 없으면 backup, 둘 다 없으면 큐 대기→timeout escalation). (ADR 0012)
_Avoid_: WorkerType(타입이 아니라 우선순위), Priority(단독 — 디스패치 결정과 혼동)

**DelegationSnapshot (위임 스냅샷)**:
owner가 백업 워커에 *명시적으로 위임한* 격리 스냅샷의 메타 — `owner_id` · `agent_ids`(위임 대상 카드, 어느 담당 영역을 백업이 답하나) · `snapshot_at`(스냅샷 뜬 시각, staleness 판정 기준). **이 레코드는 위임 사실과 최신성만 든다** — 실 지식 본체(**OKF 번들**, ADR 0013 — owner가 백업에 위임하는 본체가 곧 OKF 번들의 스냅샷)는 owner별 격리 저장소에 있고 *백업 인스턴스만* owner 키로 접근한다(중앙 무지식 보존 — 중앙·디스패처는 이 메타만 보고 실 데이터는 안 본다). `snapshot_at`은 *OKF 번들 신선도*이고, staleness 거부는 "오래된 번들로 안 답함"으로 읽힌다. AgentCard 자기보고 필드가 *아니라* 별 레코드인 이유: 위임은 가용성·보안 정책이지 담당 영역 선언이 아니고(Authority 중앙·ADR 0004 정합), opt-in이 1급으로 드러나야 위임 없는 owner가 백업 단계를 건너뛰어 곧장 escalation으로 떨어진다(폴백 사슬 정합). **staleness 정책(ADR 0012 결정 9)**: `snapshot_at`이 정책 임계를 넘으면 백업은 *신뢰를 더 낮춰 답하는 게 아니라 아예 거부*하고 escalation으로 떨어진다(너무 오래된 데이터로 답하느니 사람 — "모르면 안전하게 넘긴다"). 그래서 *답이 나가는* 백업은 모두 임계 내 fresh라 `mode`는 `backup` 하나로 충분하다(fresh/stale을 mode로 쪼개지 않음 — staleness는 *답 여부*를 가르는 메타이지 *나간 답의 mode*를 가르는 축이 아니다). `snapshot_at`은 검토 시 owner가 "얼마나 오래된 스냅샷으로 답했나" 참고하는 맥락 메타로도 `BackupReviewItem`에 실린다. 동기화 트리거는 owner 수동+주기+지식 변경 이벤트(정책, 실 파이프라인 후속). 암호화·owner 키 핸들은 *연결점만*(실 구현 후속, ADR 0009 연계). **구현(T6.6 슬라이스 ii)**: `dispatch.py`의 frozen 값 객체로, 디스패처(`WebSocketDispatcher.register_delegation`)가 *주입*받아 보관하고 backup push 직전 `_backup_allowed`가 `snapshot_at` 임계(주입 `staleness_threshold`)를 검사한다 — stale/부재면 backup 거부→큐 대기→timeout escalation. 실 데이터 본체·동기화 파이프라인은 범위 밖(메타·정책만). (ADR 0012)
_Avoid_: Backup(단독 — 워커와 혼동), Mirror, Replica

**BackupReviewItem (백업 답 검토 항목)**:
백업이 owner 이름으로 답한 한 건의 *검토 대기* 항목 — owner가 복귀해 보고·정정·승격할 대상(ADR 0012 결정 7). `ConflictCase`가 미해소 *다툼*을 담듯 이건 미검토 *백업 답*을 담는다(Owner 처리함 Inbox의 두 번째 면). `owner_id`(처리함 귀속 키) · `agent_id`(어느 담당 영역) · `question`(원 질문) · `backup_answer_text`(백업이 낸 답 본문 — owner가 정정 판단 근거로 봄, ConflictCase가 question 원문 보관하는 정신) · `ticket_id`(어느 작업의 답인가 — audit·재노출 연결 키, `item_id`로 재사용) · `snapshot_at`(그 답이 쓴 위임 스냅샷 시각, staleness 맥락) · `answered_at`(주입 clock 결정론) · `status`(`pending_review`/`reviewed`, ConflictCase.status와 같은 결) · `review`(reviewed일 때만 — `BackupReview`). pending_review→reviewed 전이는 `review_with()`가 `item_id` 보존한 새 인스턴스를 돌려준다(파괴적 변경 X — `ConflictCase.resolve()`와 같은 정신). **생성 트리거**: 백업 연결 답이 회신될 때 디스패처가 `mode=backup` 강제 하향과 *함께* `BackupReviewStore.add`를 호출한다(backup이란 사실=연결 등급이 진실이라 생성도 디스패처 책임 — 한 사건의 두 면). primary·full 답엔 생성 안 함(검토 불요). **미검토 백업 답이 처리함에 쌓이는 것 자체가** "owner가 자리를 비운 동안 자기 이름으로 나간 답"의 가시화다(다툼이 Inbox에 쌓이듯). (ADR 0012 결정 7)
_Avoid_: BackupAnswer(단독 — Answer와 혼동), ReviewTask, PendingAnswer

**BackupReview (백업 답 검토 결과)**:
owner가 미검토 백업 답에 내리는 1인칭 처분 — 세 결말 중 하나의 sealed sum("타입이 곧 상태", RoutingDecision·ConsensusOutcome·DispatchOutcome 정신). **Approve**(승인 — "이대로 맞다", 재노출 시 `mode` backup→full 승격, text 그대로) · **Correct**(정정 — owner가 새 답 발행해 기존 backup 답 대체, `corrected_text`·`sources`, 재노출 시 `mode=full` owner 실답) · **Dismiss**(무시 — "검토했고 조치 안 함", 검토 *완료* 사실은 남아 미검토와 구분). 모두 `by_owner`가 자기 책임 답을 1인칭으로 처분(검토 서비스가 `item.owner_id == by_owner` 강제 — ConsensusService가 후보 owner 강제하듯). `ConcurOnPrimary`와 같은 1인칭 표 정신. "보류"는 별 결말이 아니라 status가 여전히 `pending_review`인 것(StillOpen이 별 결말 아니라 미완 상태이듯). **Approve·Correct 모두 backup→full 신뢰 복원**(owner 검토를 거쳤으므로) — 차이는 Correct는 text까지 바뀌고 Approve는 mode만. **Precedent를 만들지 *않는다*** — 검토는 *답이 맞나*의 판단이지 *누가 담당인가*(라우팅 판례)가 아니다(담당은 이미 그 owner). 검토 *기록*은 `BackupReviewStore.history`(append-only 전이 보관소)가 담당하며, audit(절차 기록)에는 남기지 않는다 — audit은 질문→라우팅→디스패치→답의 절차 전용이고 검토 전이는 `PrecedentStore`에도 들어가지 않는다. (ADR 0012 결정 7)
_Avoid_: ReviewOutcome, Verdict, Resolution(이건 다툼 합의 결론 — 검토 결과와 구분)

**BackupReviewStore (검토 저장소)**:
미검토 `BackupReviewItem`을 보관·조회하는 포트 — `ConflictCaseStore`·`AuditLog`·`PrecedentStore`와 *같은 포트 패턴*(Protocol + `InMemoryBackupReviewStore`). `add(item)`(open_case 대응) · `get(item_id)` · `pending_for_owner(owner_id)`(=`open_for_owner`, 처리함 — owner 복귀 시 "내가 검토할 백업 답들" 조회) · `mark_reviewed(item)`(mark_resolved 대응). **`ConflictCaseStore`를 그대로 재사용하지 않고 별 포트**인 이유: 담는 값이 다르다(다툼·후보 vs 백업 답·검토) — 한 store에 두 타입을 섞으면 `open_for_owner`가 무엇을 돌려주는지 모호해지고 망라가 깨진다. 그러나 *패턴은 100% 재사용*(검증된 모양의 두 번째 인스턴스, 새 메커니즘 0). **전이 ≠ 기록** — 미검토 도메인 상태 보관이지 절차 로그(AuditLog)가 아니다. `history`(append-only)가 검토 전이의 단일 기록 보관소이며, audit은 이 역할을 맡지 않는다. (ADR 0012 결정 7)
_Avoid_: ReviewDB, Repository(단독)

**Work Queue (작업 큐)**:
중앙이 owner별로 질문 작업을 적재하는 큐. owner Worker가 연결돼 있을 때 가져가고, owner 부재/PC 꺼짐이면 작업은 *대기*한다(비동기 — 답은 즉시 보장되지 않는다). 미해소 작업의 도메인 보관소지 절차 로그가 아니다(전이 ≠ 기록 — AuditLog와 별개). 큐에 든 작업은 회신되거나 escalation되거나 둘 중 하나로 반드시 종착한다(미아 없음).
_Avoid_: Inbox(이건 Owner 처리함 — 합의 다툼, 작업 큐와 구분), Manager 큐(사람 위계 escalation), Mailbox

**RuntimeDispatcher (디스패처)**:
중앙이 owner별로 작업을 라우팅하고 답을 수집하는 포트 — `AuditLog`·`PrecedentStore`·`ConflictCaseStore`와 같은 패턴(Protocol + 구현체들). 중앙측 `dispatch(question, card) -> WorkTicket`(작업 큐 적재·추적표 즉시 반환) · `poll(ticket) -> DispatchOutcome`(회신·대기·escalation 조회), 워커측 `claim(owner_id) -> WorkTicket | None` · `submit(ticket_id, answer)`. **`ask_org`의 답 획득 경로**다 — `ask_org`는 동기 `AgentRuntime.answer`를 직접 부르지 않고 `dispatch→poll`로 `DispatchOutcome`을 얻어 `OrgReply`로 투영한다(ADR 0011 결정 4). 구현체: `InMemoryWorkQueueDispatcher`(in-process 작업 큐, 결정론 테스트·슬라이스1) · `LocalRuntimeDispatcher`(동기 런타임을 즉시-Delivered로 감싸는 즉답 다리) · `WebSocketDispatcher`(WS 전송층, 슬라이스2b — 아래). Authority·지식은 owner 환경에 있고 디스패처는 *운반·수집*만 — 답을 만들지 않는다. 포트 자체(`claim`/`submit`)는 무변경이되, `InMemoryWorkQueueDispatcher`는 합성 측(transport)의 ticket별 라우팅을 위한 큐 연산 `claimable(owner) -> list[WorkTicket]`(queued 후보 조회, 전이 없음)·`claim_ticket(ticket_id) -> bool`(FIFO 첫 작업이 아닌 *특정* 작업만 queued→claimed, head-of-line 해소)을 추가로 노출한다(`claim`의 FIFO-첫-작업과 짝, 슬라이스 ii). (ADR 0011)
_Avoid_: Router(이건 담당 결정 — 디스패처는 작업 운반), Broker, Queue(단독 — Work Queue는 데이터, 디스패처는 그 위 포트)

**LocalRuntimeDispatcher (로컬 즉답 디스패처)**:
동기 `AgentRuntime`(StubRuntime·ClaudeCodeRuntime)을 `RuntimeDispatcher` 포트로 감싸 *항상 즉시 `Delivered`*를 돌려주는 in-process 어댑터. `dispatch`가 그 자리에서 `runtime.answer`를 호출해 답을 만들고 회신 완료 상태로 두며, `poll`은 곧장 `Delivered`(그 Answer)를 반환한다 — 네트워크·워커·대기 없음(미회신·timeout 구조적 불가). `ask_org`가 디스패처만 보게 된 뒤(ADR 0011 결정 4) in-process 데모/단위 테스트가 *즉답*을 받게 하는 다리다(Routed→Answered가 한 호출에). 분산의 *디제너레이트 케이스*(워커=중앙, 큐 길이 0)이지 별 경로가 아니며, 실제 분산이 필요한 슬라이스2b `WebSocketDispatcher`가 이 자리를 대신한다 — 그때 비로소 `Pending(dispatched)` 분기가 살아난다. (`DispatchingRuntime`과 방향이 반대: 이건 동기 runtime→디스패처, 저건 디스패처→동기 answer.)
_Avoid_: SyncDispatcher, InlineRuntime

**WebSocketDispatcher (WS 전송 디스패처)**:
`InMemoryWorkQueueDispatcher`(작업 큐 도메인)를 *합성해 재사용*하고 그 위에 WebSocket 전송만 얹는 `RuntimeDispatcher` 구현(ADR 0011 결정 6, 슬라이스2b). 새 큐 도메인이 아니다 — 큐 상태기계(queued↔claimed↔answered↔expired·단조 종착·timeout escalation·owner별 격리)는 합성한 in-memory 큐가 소유하고(미아 없음·idempotency 1차 보증), WS는 `claim`/`submit`을 *전송*으로 중계할 뿐. 포트 무변경: `claim(owner_id)`의 pull은 "중앙 핸들러가 워커 대신 claim해 `PushWork`로 push"로 의미 보존(워커가 직접 호출 안 함, 트리거 주체만 이동), `submit`은 "워커가 보낸 `SubmitAnswer`를 핸들러가 받아 내부 submit 호출"로. 연결 레지스트리(owner_id→소켓 send 콜백)를 들어 dispatch 시 연결된 워커에 push(미연결이면 큐 대기=기존 `AwaitingWorker`). 실패 모드(결정 6-4): 끊김 시 `release_claims`(미회신 claimed→queued re-queue, 단조성 보존), 중복은 `ticket_id` 멱등(answered 재submit 무시 — 큐가 보장), heartbeat 생존 판정, 워커 인증 거부 hook(ADR 0009 연결점). **가용성 확장(ADR 0012, 설계)**: 연결 레지스트리를 owner당 등급별(`owner_id → {WorkerRole → send}`)로 확장해 owner PC(`primary`) 부재 시 owner 위임 백업(`backup`)으로 push를 폴백한다 — push 대상 선택만 우선순위로 바뀌고(primary→backup→큐 대기→timeout escalation) claim/submit/큐 도메인은 무변경. 백업 연결로 처리된 답은 `submit` 시 `Answer.mode`를 `backup`으로 강제 하향(백업이라는 사실은 *연결 등급*이 진실 — 워커 자기보고에 맡기지 않음). **timeout 예산(결정 8, 구현 슬라이스 ii)**: 단일 timeout을 t1(primary 대기)+t2(backup 대기) 2단으로 — primary 미연결은 *즉시* 백업, primary 연결됐으나 무응답은 t1 후 claim 회수(`stale_claims`)→backup 전환(중복 답은 `ticket_id` 멱등이 흡수, 먼저 온 답 채택·MVP 순차). 생성자에서 `t1 < timeout`을 강제한다(t1≥timeout이면 backup 단계가 조용히 무력화 — ValueError). primary 회수 표식(`_primary_exhausted`)은 *"이번 라우팅 1회 한정 그 primary 제외"*다 — push 확정 시 소비, primary 끊김(`disconnect`) 시 만료(그 느린 primary가 사라졌으므로 → primary 재연결 시 다시 primary로 복귀, ADR 8-2 primary 회복), 거부(보낼 곳 없음) 시엔 유지(같은 느린 primary로 즉시 되돌아가지 않게). **head-of-line 해소(결정 8)**: `_push_pending`이 FIFO 첫 작업 하나만 claim하던 것을 큐 도메인의 `claimable(owner)`(queued 후보 조회)+`claim_ticket(ticket_id)`(특정 작업만 claim)로 바꿔 — 거부 작업(backup 부재·stale)을 *건너뛰고* 뒤의 push 가능한 작업을 push한다(거부 작업 하나가 정상 작업을 막지 않음). 거부 작업은 claim하지 않고 queued로 남아 자기 timeout으로 escalation(미아 없음·단조성 보존). **staleness(결정 9)**: backup push 전 `DelegationSnapshot`이 ① 있어야(opt-in) ② `agent_ids`에 그 ticket의 카드가 들어야(위임 대상 영역만) ③ `snapshot_at`이 임계 내 fresh여야 push한다 — 하나라도 불통이면 push 안 하고 큐 대기→escalation(stale·대상외 거부, "모르면 넘김"). **cold 연결점(결정 10)**: warm이 MVP(백업 미리 연결)이고, cold는 디스패처 *기동 요청 hook*(`wake_backup: Callable[[str], None] | None` 주입, `manager_of` 정신)만 연결점으로 둔다 — cold의 "기동 대기"는 기존 큐 대기(`AwaitingWorker`)가 표현(새 상태 0), 기동 실패는 기존 timeout escalation 흡수. `transport.py`. **중앙 WS 핸들러**(`server.py`의 `create_worker_app(dispatcher)` + `@app.websocket("/worker")`)가 워커 아웃바운드 연결을 받아 이 디스패처로 프레임을 중계한다 — 채팅·처리함 어댑터(`web.py`)와 책임 분리. (ADR 0011 결정 6, 구현 슬라이스2b-i)
_Avoid_: Broker, WsServer(단독 — 디스패처는 포트 구현, WS 서버 핸들러는 그 위 어댑터)

**Transport Frame (전송 프레임)**:
owner 워커↔중앙 WebSocket으로 오가는 와이어 메시지 — `type` 판별 필드를 가진 봉투(envelope), pydantic v2 DTO(`transport.py`). 도메인 값 객체(`WorkTicket`·`Answer`, frozen dataclass)가 *아니라* 전송 DTO라 분리하고 경계에서 변환한다(전이 ≠ 전송). 워커→중앙(업스트림): `RegisterWorker`(owner 신원 선언·인증 연결점) · `SubmitAnswer`(답 회신, `ticket_id` 멱등) · `Heartbeat`/`Ack`(연결 생존·수신 확인). 중앙→워커(다운스트림): `Welcome`/`AuthError`(등록 수락·거부) · `PushWork`(claim한 작업 전달) · `Ping`(생존 확인). (ADR 0011 결정 6-3)
_Avoid_: Message(단독), Event, Packet

**DispatchingRuntime (동기 호환 어댑터)**:
`RuntimeDispatcher` 위에 동기 `AgentRuntime.answer`를 얹는 어댑터(ADR 0011 슬라이스1 산물) — dispatch→블로킹 poll로 동기 계약을 보존한다. 단 `ask_org`는 이를 **거치지 않는다**(ask_org는 디스패처를 직접 보고 escalation/미회신을 `Pending`으로 표면화 — ADR 0011 결정 4, "Answer 위장 금지"). 이 어댑터의 `EscalatedToManager`/`AwaitingWorker` → 폴백 `Answer` 변환은 *AgentRuntime 계약(항상 Answer 반환)을 지키기 위한 어댑터 한정 동작*이지 도메인 처분을 답으로 뭉개도 된다는 뜻이 아니다 — "동기 answer를 꼭 요구하는 비-ask_org 호출처"의 호환 경로로만 남는다(현재 그런 호출처는 단위 테스트뿐, 프로덕션 경로 아님).
_Avoid_: (ask_org의 답 경로로 오인 금지 — 그건 LocalRuntimeDispatcher)

**WorkTicket (작업 추적표)**:
중앙이 작업을 owner 큐에 넣을 때 즉시 돌려받는 추적표 — 답이 아니라 비동기 손잡이. `owner_id`(어느 owner 큐) · `agent_id`(어느 카드의 답) · `question`(워커가 로컬 claude에 넘길 원문) · `enqueued_at`(주입 clock, timeout 판정 기준) · `ticket_id`. `owner_id` 귀속이 신원/책임 연결점(회신이 진짜 그 owner에게서 왔는지, ADR 0009). 카드 본문이 아니라 식별자만 든다(`Candidate`·`ConflictCase`와 같은 정신 — 카드 출처는 Registry).
_Avoid_: Job(단독), Task(단독), Receipt

**DispatchOutcome (디스패치 결과)**:
`poll(ticket)`이 돌려주는 결말. "타입이 곧 상태"(RoutingDecision·ConsensusOutcome 정신) — 세 결말 중 하나. **Delivered**(워커가 owner 환경에서 답을 만들어 회신 → `Answer` 도착, `mode` 보존 = Approval 게이트 합류 자리) · **AwaitingWorker**(아직 회신 없음 — 워커 미연결/생성 중 → 큐에 대기, `waited` 경과시간) · **EscalatedToManager**(timeout/owner 부재 → 미아·합의 실패와 같은 종착 처분 = Escalation. `manager_id`는 Manager 큐가 *기계로 소비*할 1급 식별자 — owner의 `manages` 상위 User.id(루트면 `None`), 어느 큐에 적재할지. `reason`은 그 escalation의 *사람용* 자연어 근거. 둘을 분리: 큐 라우팅은 식별자로, 운영자 화면은 문장으로 — `ConsensusOutcome.Deadlocked.reason`이 사람용이듯). owner 부재·timeout escalation은 새 경로가 아니라 사람 그래프 상향(Owner→Manager) 재사용이며, **Manager 큐로 수렴**한다 — `ManagerItem`의 `EscalationSource.FromDispatch`로 적재돼 Manager가 Reroute(재지정)·처리(ADR 0014. T6.3 당시 "자리만"이던 것을 T5.2가 닫음). `manager_id`가 None(루트 owner)이면 적재자가 root로 보정(미아 없음). 사용자向 투영: `AwaitingWorker`·`EscalatedToManager` 둘 다 `OrgReply.Pending(kind="dispatched")`로 모인다(`manager_id`·`reason`은 사용자에게 감추는 내부값 — Pending에 싣지 않음). (ADR 0011)
_Avoid_: Result(단독 — Answer와 혼동), DeliveryStatus

**Registry**:
Agent Card를 등록·보관·조회하는 모듈. 카드의 출처(YAML 파일)다.
_Avoid_: Directory, Store, 중앙 서버

**Router**:
질문을 받아 등록된 Agent Card들과 대조해 담당 후보를 결정하는 모듈.
_Avoid_: Broker, Dispatcher, 중앙 서버

### Routing outcomes

**RoutingDecision**:
라우터가 한 질문에 내리는 결과. 세 처분(disposition) 중 하나다 — **Routed / Contested / Unowned**. 인스턴스의 *타입*이 곧 상태이며, 각 타입은 자기 처분에 필요한 필드만 갖는다(속성값을 들여다보지 않고 타입으로 판별). **세 변이 모두 `intent: str = ""`를 든다(ADR 0015)** — 이 결정이 어떤 분류 라벨에서 나왔나. `router.route`가 classify 1회 결과를 자기가 내는 결정에 실어 *라우팅 intent 단일 출처*로 만든다(ask_org가 따로 classify 안 함). 기본값 `""`이라 기존 직접 생성처·match가 무영향(router만 채움). 래퍼(`ClassifiedDecision`)가 아니라 필드로 둔 이유: 래퍼는 반환 타입·모든 match 사이트를 바꿔 침습적이고 "타입이 곧 상태"를 흐린다 — intent는 결정의 *부속 사실*이지 결정을 감싸는 컨테이너가 아니다.

**Routed**:
담당(primary)이 정해진 결정. Collaborator·Approval을 동반할 수 있다 — `primary`(담당 카드) · `confidence` · `reason` · `requires_approval: bool`(Approval 게이트) · `collaborators: tuple[AgentCard, ...]`(끌어들인 협업 카드, primary 제외). Route(primary)·Collaboration(collaborators)·Approval(requires_approval)은 한 Routed 안 *동시 성립 독립 축*이고, 셋이 다 비어도 Routed는 성립한다(필드 기본값 빈 튜플·False — 하위호환). **부착 규칙(intent 기반 결정론, TRD §6 5단계, T2.5)**: Router가 `primary.approval_when`에 intent가 들면 `requires_approval=True`, `primary.collaborate_when`에 intent가 들면 *그 intent를 domains에 가진 다른 카드*를 collaborator로 끌어들인다(카드가 agent_id로 지목하지 않고 라우팅과 같은 domains 매칭 재사용 — 카드 자기보고 보수성·중앙 외 Authority 선언 회피). `approval_when`·`collaborate_when`은 under-claim 자기보고(ADR 0004 정합 — owner가 "사람 사인/협업 필요"로 *스스로 좁히는* 보수 신호). 부착은 `Router._attach_gates`가 Routed 생성처(판례·단일 매칭) 공통으로 수행.

**Contested**:
후보가 여럿이라 담당이 아직 안 정해진 결정. 후보 Owner 합의 또는 Manager로 간다.

**Unowned**:
후보가 0인 미아 결정. 루트 User로 Escalation된다.

**Route**:
질문을 primary Agent Card에 연결하는 진입 행위(= Routed를 만드는 것).

**Candidate**:
질문의 domains에 부분적으로 매칭되어 담당 가능성이 있는 Agent Card. 0·1·다수.

**Intent**:
질문을 분류해 얻은 주제 라벨. Router가 카드의 `domains`와 대조하는 키다. **Classifier** 포트가 질문에서 생성한다(v0 규칙 기반 `RuleBasedClassifier` → LLM `LlmClassifier` T6.2). **한 질문 처리에는 라우팅 intent가 단 하나다(ADR 0015)** — `router.route`가 classify를 1회 하고 그 intent를 자기가 내는 `RoutingDecision`(세 변이 모두)에 싣는다. `ask_org`는 자기 classify로 따로 구하지 않고 `decision.intent`를 읽어(ConflictCase·AuditEntry) 두 분류 호출 divergence를 차단한다(결정론 분류기는 우연히 일치하나, 비결정 LLM 분류 도입 시 갈려 케이스 라벨·audit이 라우팅과 어긋나는 상관관계 버그). intent는 *조직 내부값*이라 사용자向 OrgReply엔 싣지 않는다(노출 불변식 — decision→OrgReply 투영이 떨굼).
_Avoid_: Topic, Category

**Collaboration / Collaborator**:
primary는 그대로 두고 추가로 끌어들이는 협업. 끌려 들어온 Card가 Collaborator. (기획서 `required_handoffs` 필드 → `collaborators`로 개명.)

**Approval (승인 게이트)**:
Routed인데 실행 전 사람 사인이 필요한 상태. 라우팅은 됐고 게이트만 걸린다. (기획서 `human_approval_required`) 게이트 *표시*까지 구현됨(`Routed.requires_approval`→`AskOrg._apply_approval_gate`가 `mode="draft_only"`로 답을 내림, T2.5). 실 승인 행위(draft→full로 푸는 사인)는 **Manager 큐와 별 탭/행위로 후속 분리**(ADR 0014 결정 4) — Approval은 *담당 정해짐+사인 대기* 게이트라 *담당 미정* escalation(Manager 큐 수렴)과 도메인이 다르고 승인자도 manager 전용이 아니다. 같은 운영 화면에 탭으로 나란히 둘 수 있으나(owner 처리함이 합의·검토 두 탭을 갖듯) 도메인은 분리.
_Avoid_: Escalation(이건 게이트가 아니다)

**Escalation**:
담당 자체를 사람(Manager)이 정하도록 결정을 사람에게 넘기는 것 — Contested(합의 실패)·Unowned·owner 부재/timeout의 종착 처분. Approval(게이트)과 구분. **Manager 큐 적재로 종착**(ADR 0014) — 세 출처(`Unowned`·`Deadlocked`·`EscalatedToManager`)가 하나의 `ManagerItem`로 수렴해 `pending_for_manager`에 쌓여 사람을 기다린다(미아 없음의 마지막 칸). 처분 *상태*만 남던 것이 큐 적재로 *사람 손에 닿는다*.

**Transfer (이관)**:
이미 배정된 primary를 다른 Card로 재지정하는 런타임 사건. A 빠지고 B 맡음. 전이이며, 기록은 감사 로그가 맡는다(전이 ≠ 기록).
_Avoid_: Handoff(모호 — 프레임워크 용어·기록 의미와 혼동), Delegate, Forward

> 한 **Routed** 안에서 Route(primary)·Collaboration(collaborators)·Approval(게이트)은 동시에 성립 가능한 독립 축이다. **Escalation**은 Contested/Unowned의 종착이고, **Transfer**는 배정된 primary를 사후에 바꾸는 런타임 사건이다.

### User-facing outcome (실 사용자向 투영)

라우팅 기계장치(RoutingDecision·Candidate·Confidence·trace)는 실 사용자에게 감춘다. 사용자는 *처분의 결과*만 사용자 말로 받는다. `ask_org` 핸들러가 `RoutingDecision`을 아래 결과 타입으로 투영한다 — 도메인(RoutingDecision)과 표현(OrgReply)의 경계.

**OrgReply**:
실 사용자가 `ask_org`에서 돌려받는 결과. `RoutingDecision`과 `DispatchOutcome`의 사용자向 투영이며, 두 형태로 모인다 — **Answered**(담당이 답함 = `Routed`→`Delivered`) · **Pending**(아직 답이 없음 — 사람 손으로 넘어갔거나 담당이 답을 만드는 중). 인스턴스의 *타입*이 곧 사용자가 받은 상태다. 노출 불변식: 담당·승인 상태·출처만 보이고, confidence·trace·후보·manager_id·reason 등 *조직 내부 구조*는 절대 싣지 않는다 — 단 답 회수를 위한 **불투명 추적 토큰 1개**는 예외(ADR 0011 결정 6-5, 슬라이스2b — 사용자가 그것으로 답을 조회. 토큰은 내부 구조를 비추지 않는 ID라 노출 불변식의 *정밀화*이지 완화가 아니다).
_Avoid_: Response, Result(단독 — Answer와 혼동)

**Answered**:
Routed가 투영된 결과. `text`(Answer 본문) · `answered_by`(primary의 owner·agent_id) · `mode`(`full`/`draft_only`/`backup`) · `sources`를 노출. **Approval 게이트 강제(T2.5, ADR 0012 mode 강제 패턴)**: `Routed.requires_approval`이면 `ask_org`가 답을 `mode="draft_only"`로 내려 "초안·승인 대기"를 표시한다 — 워커 자기보고가 아니라 *라우팅 결정*이 강제하고(디스패처가 backup을 강제 하향하는 정신), 강제 자리는 `AskOrg._apply_approval_gate`(즉답 Delivered 경로). mode 우선순위: `backup`은 draft_only로 덮지 않는다(backup이 더 강한 하향 — owner 미검토 답이라 승인 대기보다 약한 신뢰가 맞다), `full`→`draft_only`만 격상. **collaborators는 Answered에 *싣지 않는다*** — 노출 불변식(담당·승인 상태·출처만)상 collaborator는 조직 내부 협업 구조라 사용자向에 비추지 않는다(audit이 `Routed.collaborators` 원형으로 보관). T2.5는 *게이트 표시*까지 — 실 승인 행위(draft→full)는 T5.2 Manager 큐. **분산 회신(retrieve) 경로의 Approval 강제는 후속** — `_tracking`이 `requires_approval`을 안 들어 자리 추가가 선결이고, 데모는 `LocalRuntimeDispatcher` 즉답이라 즉답 경로로 PRD §7 시나리오 2가 시연된다.

**Pending**:
즉답이 없는 상태의 사용자向 투영 — 담당이 사람 손에 있거나(Contested·Unowned), 담당이 정해졌으나 분산 전송으로 답을 기다리는/사람으로 넘어가는 중(dispatched). `kind`(`contested`/`unowned`/`dispatched`)와 사용자向 안내 문구만 노출하고, 후보 목록·escalation 대상·`manager_id`·`reason`·`ticket_id` 같은 내부는 감춘다. Contested는 *다툼이라는 사실조차* 감추고 "담당을 확인하는 중" 류 중립 안내만(후보가 여럿이라는 내부를 비추지 않는다), Unowned는 "아직 담당이 없어 매니저에게 전달" 류. **dispatched**는 `DispatchOutcome`의 `AwaitingWorker`(미회신)와 `EscalatedToManager`(timeout/owner 부재)를 *함께* 투영한다 — 둘은 도메인에선 다른 처분이지만 사용자 관점에선 "담당에게 보냈는데 아직 답이 없다"로 동일하고, 워커 미연결인지 Manager escalation인지는 감춰야 할 내부값이라 한 kind로 모은다(쪼개면 내부 구분이 샌다). "담당에게 전달했고 답변이 준비되면 알림" 류 중립 안내만. 답 회수(ADR 0011 결정 6-5, 슬라이스2b-i 구현): `dispatched`는 *불투명 추적 토큰*(`tracking`)을 동반해(internal 구조 노출 없는 ID 1개) 사용자/데모 UI가 그 토큰으로 답을 *조회*(pull)한다 — 워커가 나중에 회신한 실 claude 답이 사용자에게 도달하는 경로. **구현 방침**: 서버(`AskOrg._tracking`)가 `tracking→WorkTicket` 매핑을 보관하고, 토큰은 `ticket_id`와 *분리된* 별도 `uuid4().hex`다(ticket_id조차 노출하지 않음 — 6-5의 "서버가 ticket 보관" 대안 채택). 조회는 `AskOrg.retrieve(tracking)`(`poll` 재노출) → web `GET /ask/{tracking}`. 사용자向 푸시(SSE/WS)는 범위 밖(조회로 한정).
_Avoid_: Escalation(이건 도메인 처분, Pending은 그 사용자向 표현)

### Conflict & learning

**Authority (관할)**:
다툼이 나는 영역의 최종 결정권. 중앙 라우팅 규칙에서 선언되며 카드가 자기보고할 수 없다.
_Avoid_: Trust, Priority(단독)

**Conflict**:
한 사안의 담당이 깨끗하게 정해지지 않는 상태. 두 극과 각각의 해소 경로 —
- **Overlap**(후보 여럿): 후보 Owner 전원을 호출해 당사자끼리 담당을 합의. 합의 실패 시 Manager로.
- **Gap**(후보 0, 미아 질문): Manager가 기존 Owner 지정 또는 신규 Agent Card 생성.
두 경로 모두 결론은 Resolution → Precedent로 떨어진다.

**ConflictCase (다툼 케이스)**:
미해소 Overlap 다툼을 *조회 가능한 상태*로 저장한 단위. `Contested`는 라우터의 순간 판정일 뿐이라 후보 Owner가 1인칭 합의하려면 그 다툼이 머물 곳이 필요하다 — 그게 ConflictCase. `intent`(어떤 분류 라벨의 다툼) · `question`(원문, Owner가 처리함에서 맥락 판단) · `candidates`(다툼에 걸린 **Candidate** = `agent_id`+`owner` 식별자만, 카드 본문은 Registry가 출처) · `status`(`open`/`resolved`) · `opened_at`(주입 clock 결정론) · `case_id` · `resolution`(resolved일 때만). open→resolved 전이는 `resolve()`가 `case_id` 보존한 새 인스턴스를 돌려준다(파괴적 변경 X). `Contested.candidates`(AgentCard 객체)와 달리 식별자만 들고, owner를 함께 드는 이유는 **Owner별 처리함 조회**가 본질이라서. (ADR 0008)
_Avoid_: Ticket, Dispute, Issue

**Inbox (처리함)**:
한 Owner에게 귀속된 미해소 항목들의 모음 — PRD 페르소나의 "Owner 처리함" 그 면이다. **세 면(탭)을 가진다**: ① 자기 카드가 후보로 걸린 open ConflictCase들(다툼 합의 — 데이터 원천 **ConflictCaseStore**.`open_for_owner`), ② owner 부재 중 백업이 자기 이름으로 답한 미검토 백업 답들(검토 — 데이터 원천 **BackupReviewStore**.`pending_for_owner`, ADR 0012 결정 7), ③ OKF 변경(커밋)이 stale로 표식한 과거 Precedent·답들(재평가 — 데이터 원천 **ReevalStore**.`pending_for_owner`, ADR 0019). 셋 다 "Owner별 미해소 항목 보관·조회"라는 한 패턴의 인스턴스이며, 별도 영속 상태가 아니라 각 store의 open/pending 투영이다. Owner는 ①에서 1인칭 합의 표(ConcurOnPrimary)를 던지고, ②에서 백업 답을 1인칭으로 처분(BackupReview — 승인/정정/무시)하고, ③에서 stale 판례·답을 1인칭으로 처분(ReevalOutcome — Keep/Invalidate/Supersede Precedent · Acknowledge/ReAnswer)한다. ②가 결정 5의 "백업 답도 owner 책임"을, ③이 ADR 0017 결정 3②의 "정책 변경 시 과거 판례·답 재검토"를 명목에서 실질로 만든다(owner가 복귀하면 자기 이름의 답·자기 판례를 반드시 마주한다). **pull에 push 추가(T7.4·ADR 0022)**: 세 면은 모두 owner가 *조회(pull)*하는 면이다 — T7.4는 각 면에 항목이 *적재되는 사건*에서 owner에게 push 통지(`Notifier`·`NotificationChannel`)를 더한다(pull은 그대로·push는 추가 — 통지 실패해도 pull로 일을 봐 미아 없음). 통지 발화는 적재 지점의 `notifier` 옵셔널 주입(처리함 자체는 무변경).
_Avoid_: Queue(이건 Manager 큐 — 사람 위계 escalation, 처리함과 구분), Tasklist

**ConflictCaseStore (케이스 저장소)**:
open ConflictCase를 보관·조회하는 포트 — `AuditLog`·`PrecedentStore`와 같은 패턴(Protocol + `InMemoryConflictCaseStore`). `open_for_owner(owner)`(처리함) · `open_for_intent(intent)`(중복 open 방지 선조회) · `mark_resolved(case)`(open에서 빼 history에 append) · `get(case_id)` · `open_case(case)`. **전이 ≠ 기록** — 미해소 도메인 상태를 보관하는 곳이지 운영자向 절차 로그(AuditLog)가 아니다. 같은 intent로 이미 open된 케이스가 있으면 새로 만들지 않는다(같은 다툼이 질문마다 케이스를 양산하지 않게).
_Avoid_: CaseDB, Repository(단독)

**ConcurOnPrimary (합의 표)**:
후보 Owner 한 명의 *1인칭* 합의 입력. `by_owner`(표를 던진 Owner `User.id`)가 `on_agent`(primary로 지목한 카드 `agent_id`)를 담당으로 지목 + `rationale`(근거, 선택). claim("내가 맡는다"=자기 카드 지목)과 concede("쟤가 맡아"=남 지목)를 **같은 한 축**(primary는 누구)으로 환원한 단일 표 — 찬반 2축·라운드·코멘트 스레드는 후순위. 후보 Owner 전원이 같은 `on_agent`를 지목하면 합의 성립 → Resolution. (ADR 0008)
_Avoid_: Vote(단독 — 다수결 아님, 전원 일치), Claim/Concede(둘로 쪼개지 않고 한 축으로)

**ConsensusOutcome (합의 결과)**:
후보 Owner들의 ConcurOnPrimary 표를 모아 합의를 시도한 결과. "타입이 곧 상태"(RoutingDecision·OrgReply 정신) — 세 결말 중 하나. **Agreed**(전원 일치 → `Resolution`+`Precedent` 산출, 케이스 closed) · **StillOpen**(표가 덜 모임 → 케이스 open 유지, `pending_owners` 남음) · **Deadlocked**(표가 갈림=교착 → 합의 실패. **Manager 큐로 수렴** — `ManagerItem`의 `EscalationSource.FromDeadlock`으로 적재돼 Manager가 중재(AssignOwner→Resolution→Precedent로 그 ConflictCase 종결), ADR 0014. T4.2 당시 "자리만"이던 것을 T5.2가 닫음). T4.2의 핵심은 Agreed→Precedent이며, Deadlocked의 사람 종결은 Manager 큐가 받는다.
_Avoid_: Result(단독 — Answer와 혼동)

**Manager**:
다른 User를 `manages` 하는 User(조직장). 사람 위계의 상위 노드이며 Owner와 무관 — 에이전트를 소유할 수도, 안 할 수도 있다. 미해소 Conflict의 escalation은 카드가 아니라 사람 그래프를 타고 Owner의 manager로 올라가며, 꼭대기는 루트 User.
_Avoid_: Admin(단독), Triage, "Owner의 일종"

**Manager 큐 (Manager queue)**:
한 Manager에게 귀속된 미해소 escalation들의 모음 — PRD §4 페르소나의 "Manager 큐" 그 면이다(승인·escalation·합의 실패를 처리). **세 출처가 하나의 `ManagerItem`로 수렴**한다(ADR 0014) — `RoutingDecision.Unowned`(미아)·`ConsensusOutcome.Deadlocked`(합의 교착)·`DispatchOutcome.EscalatedToManager`(owner 부재/timeout). 셋 모두 *"담당/답을 사람이 정해야 하는 한 escalation"*이라 한 큐로 모으되, 출처 차이는 `EscalationSource` sealed sum으로 담는다(Owner 처리함이 다툼·검토를 *별 store*로 가른 것과 다른 판단 — 거긴 다른 일, 여긴 같은 일의 다른 출처). 색인 키가 **`manager_id`**(owner가 아니라 그 위 사람 — escalation은 owner가 아니라 manager에게 귀속). escalation의 Manager 큐 적재 = **미아 없음의 최종 종착**(처분 상태만 남던 마지막 칸을 채움 — 반드시 누군가의 큐에 닿고 root로 보정). 운영 면이라 내부값 노출 OK(채팅 OrgReply 불변식과 다른 면). (ADR 0014)
_Avoid_: Inbox(이건 **Owner 처리함** — 합의·검토, Manager 큐와 구분), Work Queue(이건 owner별 작업 큐 — 분산 전송, 사람 위계 escalation과 구분), Mailbox, Triage

**ManagerItem (Manager 큐 항목)**:
Manager 큐에 쌓인 한 escalation 항목 — `ConflictCase`가 미해소 *다툼*을, `BackupReviewItem`이 미검토 *백업 답*을 담듯 이건 미해소 *escalation*을 담는다(Manager 처리함의 한 항목). `manager_id`(귀속 키 — 어느 Manager 큐, `pending_for_manager` 색인) · `source`(**EscalationSource** — 출처 원형) · `created_at`(주입 clock 결정론) · `item_id` · `status`(`open`/`resolved`, ConflictCase.status 결) · `resolution`(resolved일 때만 — **ManagerResolution**). open→resolved 전이는 `resolve()`가 `item_id`·source 보존한 새 인스턴스를 돌려준다(파괴적 변경 X — `ConflictCase.resolve()`·`BackupReviewItem.review_with()`와 같은 정신). manager_id는 끝내 None이 안 된다(적재자가 root 보정 — 미아 없음). (ADR 0014)
_Avoid_: EscalationTicket, Case(이건 ConflictCase — 다툼), Task

**EscalationSource (escalation 출처)**:
한 `ManagerItem`이 어디서 왔나 — 세 출처의 sealed sum("타입이 곧 상태", RoutingDecision·ConsensusOutcome·DispatchOutcome 정신). **FromUnowned**(미아 → `decision`(Unowned 원형)·`question`; manager_id=root) · **FromDeadlock**(합의 교착 → `case`(ConflictCase 원형)·`reason`; manager_id=후보 Owner의 manager) · **FromDispatch**(owner 부재 → `outcome`(EscalatedToManager 원형); manager_id=outcome.manager_id). 각 출처가 *원형 처분을 그대로 안아* Manager가 전체 맥락(후보·owner/agent_id·root)을 본다(audit이 decision·dispatch_outcome 원형을 안는 정신). FromUnowned·FromDeadlock은 *담당 미정* escalation, FromDispatch는 *담당 정해졌으나 답 부재*(가용성) escalation이라 처리 행위가 갈린다. (ADR 0014)
_Avoid_: EscalationKind, EscalationType, Reason(단독 — 사람용 자연어와 혼동)

**ManagerAction (Manager 처분)**:
Manager가 한 항목에 내리는 1인칭 처분 — sealed sum(`ConcurOnPrimary`·`BackupReview` 정신, `by_manager` 1인칭). **AssignOwner**(담당 지정 — 미아·교착의 사람 종결; intent 있으면 `Resolution`(intent→primary)→**Precedent 학습**, CONTEXT Conflict "두 경로 모두 Resolution→Precedent"; FromDeadlock이면 그 ConflictCase도 resolved) · **Reroute**(재지정 — owner 부재의 운영 판단, **Transfer**에 해당, Precedent 안 만듦 — 일회 가용성 사건이지 담당 규칙 변경 아님) · **Dismiss**(종결 — "확인했고 조치 안 함", 처리 완료 사실은 남김, Precedent X). 처리 서비스(`ManagerQueueService`)가 `item.manager_id == by_manager` 강제(ConsensusService·BackupReviewService 정신). T5.2는 자리+기본 처리까지 — 출처-행위 적합성 강제·멀티홉·처리 시 자동 통지·신규 카드 생성은 후속. (ADR 0014)
_Avoid_: ManagerDecision, Verdict, Triage(단독)

**ManagerResolution (Manager 처리 결론)**:
한 `ManagerItem`의 처리 결말 — `action`(Manager가 내린 ManagerAction 원형, by_manager 보존) · `resolution`(AssignOwner가 Precedent로 흘린 conflict **Resolution**; Reroute/Dismiss면 None). conflict `Resolution`(intent→primary, 합의 결론)과 *구분* — ManagerResolution은 "이 escalation을 사람이 이렇게 종결했다"는 큐 항목의 결말이고 그 *안에* (AssignOwner면) conflict Resolution을 담는다. (ADR 0014)
_Avoid_: Resolution(단독 — 이건 다툼 합의 결론, ManagerResolution은 큐 항목 결말)

**ManagerQueueStore (Manager 큐 저장소)**:
미해소 `ManagerItem`을 보관·조회하는 포트 — `ConflictCaseStore`·`BackupReviewStore`·`AuditLog`·`PrecedentStore`와 *같은 포트 패턴*(Protocol + `InMemoryManagerQueueStore`)의 세 번째 인스턴스. `enqueue(item)`(open_case/add 대응) · `get(item_id)` · `pending_for_manager(manager_id)`(=`open_for_owner`/`pending_for_owner`, Manager 처리함 — "내 큐에 쌓인 escalation들") · `get_by_case(case_id)`(FromDeadlock 중복 적재 방지 — 같은 ConflictCase가 두 번 안 들게, `open_for_intent` 정신) · `mark_resolved(item)`. **색인 키가 `owner`가 아니라 `manager_id`** — escalation은 owner가 아니라 그 위 사람에게 귀속(ConflictCaseStore·BackupReviewStore와 그 점만 다름, 패턴은 100% 재사용). **전이 ≠ 기록** — 미해소 escalation 도메인 상태 보관이지 절차 로그(AuditLog)가 아니다(escalation은 audit에 이미 남음 — ADR 0011 결정 5). (ADR 0014)
_Avoid_: QueueDB, EscalationStore(단독), Repository(단독)

**Resolution**:
한 Conflict에 대해 관련 Owner들(필요시 사람 Authority)이 합의한 결론. `frozen` 값 객체 — `intent`(어떤 분류 라벨의 다툼인지) · `primary`(합의로 정해진 담당 `agent_id`) · `rationale`(합의 근거, 선택). "타입이 곧 상태"라 이것 자체가 합의됐다는 사실이다. Overlap에선 ConflictCase의 후보 Owner들이 ConcurOnPrimary 표로 전원 일치할 때 산출되고(`ConsensusOutcome.Agreed`), 곧장 Precedent로 기록되며 그 ConflictCase는 resolved된다.

**Precedent (판례)**:
Resolution을 append-only로 남긴 기록. 라우터가 유사한 미래 케이스에 참조한다. 구조화된 조직 암묵지이자 곧 라우팅 회귀 테스트 케이스. `frozen` 값 객체 — `resolution`(무엇을 합의했나) · `recorded_at`(언제 기록됐나, 주입 clock 기반 결정론) · `needs_review: bool=False`·`last_flagged_at: datetime|None=None`(**신선도 신호**, ADR 0019 — 변경 전파기가 OKF 커밋 변경 이벤트로 *과거 판례*에 stale을 표식; frozen·하위호환 기본값) · `invalidated: bool=False`·`invalidated_at: datetime|None=None`·`invalidated_by: str|None=None`(**무효 신호**, ADR 0019 결정 6·T8.4(d) — owner가 `InvalidatePrecedent`로 명시 처분한 *뒤* `invalidate`가 다는 표식; frozen·하위호환·append-only). **`status: Literal['valid',...]`는 안 쓴다** — 'valid'가 admission 어휘("유효하지 않은 카드는 등록 안 됨")와 충돌·자기모순이고, stale은 *재검토 대상*이지 *무효*가 아니라 boolean이 정확하다. **stale ≠ 무효화(독립 축)**: `needs_review`(stale·재검토 대상·라우팅 *유지*)와 `invalidated`(무효·라우팅 *제외*)는 서로 안 덮어쓰는 독립 축이다. Router lookup은 `needs_review`를 *보지 않아* stale 판례도 계속 라우팅되고(미아 없음 보존), `invalidated`면 판례 단축경로를 건너뛰고 분류기 폴백한다(아래 제외 지점 안 B). 무효화는 owner 1인칭 처분(`InvalidatePrecedent`) 후만(ADR 0019 결정 6). **PrecedentStore** 포트로 보관한다 — `record(resolution) -> Precedent`(append) · `lookup(intent) -> Precedent | None`(라우터 조회·*순수 읽기* — invalidated 판례도 그대로 반환·라우팅 제외는 Router가 판단) · `find_by_primary(agent_id) -> list[Precedent]`(그 agent를 primary로 둔 판례 — 변경 영향 식별, `_by_primary` 역색인) · `list_all() -> list[Precedent]` · `flag_stale(intent, trigger_sha, at) -> Precedent | None`(stale 표식·멱등·append-only, ADR 0019) · `invalidate(intent, by_owner, at) -> Precedent | None`(무효 표식·flag_stale 동형 — 판례 없으면 None·이미 invalidated면 멱등·새 인스턴스 교체+history append+`_by_primary` 동기화·**store 삭제 X**로 list_all/find_by_primary 보존, ADR 0019 결정 6·T8.4(d)). 감사 로그와 같은 append-only 메커니즘이되, 운영자向 절차 기록(AuditLog)과 달리 라우터向 intent-색인 조회 사실이라 포트를 분리한다(ADR 0002 보강). **무효 라우팅 제외 지점 = Router 단일 지점(안 B)**: `lookup`은 순수 읽기로 두고(운영 면 읽기·`find_by_primary`/`list_all`과 일관) Router가 `p.invalidated`를 보고 판례 경로를 건너뛰어 분류기 폴백(0→Unowned·1→Routed·≥2→Contested, 항상 종착 — 미아 없음). `needs_review` 해석과 *대칭*(판례 플래그→라우팅 해석을 Router가 결정).
_Avoid_: Rule(단독), History

**OkfChangeEvent · StalenessPropagator (변경 전파)**:
OKF 커밋이 곧 변경 사건이라는 진실 원천(ADR 0017 결정 3②·ADR 0019). **OkfChangeEvent**(`git_gateway.py`·frozen): `agent_id`(어느 번들)·`new_sha`·`parent_sha`(커밋 직전 HEAD·최초면 None)·`changed_paths`·`author`·`committed_at`(주입 clock). 단일 발화 지점은 `commit_okf_bundle(req, gateway, propagator=None)` — propagator 옵셔널 주입(None=기존 동작·하위호환), 비None이면 커밋 직후 `on_okf_committed(event)` 1회. `CommitResult`·web 응답(`{sha,agent_id}`)은 불변(노출 불변식). **`changed_paths`·`parent_sha`는 이벤트에 싣되 MVP 영향 식별엔 안 쓴다**(죽은 필드·미래 정밀화 자리 — agent_id 단위 거친 매칭, `sources`↔`changed_paths` 교차는 타입 불일치 과소검출이라 기각). **StalenessPropagator**(`reeval.py`): 변경 이벤트로 영향 Precedent·답을 식별(① `find_by_primary` 역색인 ② audit `records()` dict 순회 — `decision.disposition=='routed'`·`primary==agent_id`·`snapshot_sha != HEAD or None` 보수 매칭, 과검출 허용·놓침 0)해 stale 표식·ReevalStore 적재. **이미 `needs_review`거나 `invalidated`(owner 무효 처분·T8.4(d)) 판례는 재적재에서 뺀다**("이미 처리됨" 가드 — owner가 닫은 판례를 또 묻는 처리함 노이즈 방지). 새 통지 인프라 0 — 적재가 곧 처리함 nudge(실시간 push는 T7.4).
_Avoid_: Webhook(단독·실 전송 메커니즘과 혼동), Invalidation(자동 무효화 아님 — 플래그)

**ReevalSubject · ReevalItem · ReevalStore · ReevalService · ReevalOutcome (재평가 루프)**:
OKF 변경이 stale로 표식한 과거 Precedent·답을 owner가 1인칭 재평가하는 루프(ADR 0019 — Owner 처리함 *세 번째 면*). `ConflictCase`/`BackupReviewItem` 처리함 패턴의 N번째 인스턴스(Protocol + InMemory·owner 색인·불변 전이·전이≠기록). **ReevalSubject** sealed sum(*대상* 축·T8.4(a)·ADR 0019 결정 7): *무엇*이 재평가 대상인가 — `PrecedentSubject(intent)`(대상=과거 Precedent·`intent`가 PrecedentStore 키) | `AnswerSubject(audit_index: int)`(대상=과거 답·`audit_index`는 audit 기록순 0-based 정수 인덱스). 타입이 곧 대상 판별자(`RoutingDecision`·`EscalationSource` 관용구·`match`+`assert_never` 망라). 각 arm은 통지 멱등 키를 타입에서 도출하는 `notification_ref() -> str`을 든다(`precedent:{intent}` / `answer:{audit_index}` — m1 우회 구조적 해소·ADR 0022). **ReevalSubject(대상)와 ReevalOutcome(결과)는 독립 축**이다(섞지 않는다). **ReevalItem**(frozen): `subject`(ReevalSubject sealed sum — 단일 필드)·`owner_id`(처리함 귀속)·`agent_id`·`trigger_sha`(=`event.new_sha`)·`flagged_at`·`status`(pending_review/reviewed)·`review`(reviewed일 때만)·`item_id`. pending_review→reviewed 전이는 `review_with()`가 item_id 보존한 새 인스턴스(BackupReviewItem 동형). **ReevalOutcome** sealed sum(*결과* 축·arm 명명 확정·ADR 0019 결정 6): Precedent 대상 `KeepPrecedent`|`InvalidatePrecedent`(무효 의사 → **실 라우팅 제외**, T8.4(d))|`SupersedePrecedent`(새 Resolution record·삭제 없는 갱신·실 실행 후속), Answer 대상 `AcknowledgeAnswer`|`ReAnswer`(재답변 필요·실 재실행 후속). **ReevalStore**(Protocol+`InMemoryReevalStore`): `add`·`get`·`pending_for_owner`(처리함)·`mark_reviewed` + append-only `history`. **ReevalService**: 1인칭 강제(`review.by_owner==item.owner_id`·BackupReviewService 정신). **InvalidatePrecedent 실 제외(T8.4(d))**: 옵셔널 `precedents: PrecedentStore|None`·`clock` 주입(미주입이면 기존 동작·하위호환·게이트 보존). `review`가 전이 *뒤* outcome=`InvalidatePrecedent`*이고* subject=`PrecedentSubject`인 짝일 때만 `precedents.invalidate(subject.intent, by_owner, at)`로 그 판례를 라우팅에서 뺀다(`ConsensusService`의 `Agreed`→`record` *대칭*). **subject↔outcome 축 정합**: 두 축은 독립(결정 7)이라 어긋난 짝(예: `AnswerSubject`에 `InvalidatePrecedent`)이 타입상 가능 — ReevalService는 어긋난 짝을 *무시*한다(에러 X·전이는 그대로라 reviewed 종착·미아 없음). 축 정합 1차 방어선은 발화 지점(StalenessPropagator)이 구조적으로 보장. 노출 불변식: needs_review·trigger_sha·ReevalItem은 운영 면만(처리함·모니터링), 사용자向 Answered 미노출(snapshot_sha조차 안 싣는 경계 동형).
_Avoid_: Queue(Manager 큐와 혼동 — 재평가는 owner 1인칭 처리함), Review(BackupReview와 혼동 — 거긴 백업 답, 여긴 stale 판례·답)

**NotificationChannel · Notification · Notifier · FakeChannel (실시간 푸시 통지)**:
처리함/큐에 항목이 *적재되는 사건*에서 owner/manager에게 push 통지를 쏘는 도메인(ADR 0022·T7.4 구현 완료). 지금 owner·manager는 처리함·큐를 *조회(pull)*해야 새 일을 안다 — T7.4는 "새 일이 생기면 *push*로도 알린다"를 *추가*한다. **push는 pull을 대체하지 않고 추가한다** — 통지 채널이 통째로 실패해도 처리함 pull은 그대로라 미아가 없다(가시성 이중 보장 — 미아 없음은 push가 아니라 pull이 떠받친다). **NotificationChannel**(`notify.py`·Protocol): `send(notification) -> None`(fire-and-forget — 전달 결과 미반환·전송 실패는 채널 내부 재시도/미전달 자리). 실 전송은 비결정·외부 의존이라 `GitGateway`·`OidcProvider`·`AgentRuntime`과 **같은 포트 패턴** — 결정론 `FakeChannel`(메모리 inbox `recipient_id→list[Notification]`·전달 로그·게이트 내 단위 테스트) + 실 어댑터(`SlackChannel`·`EmailChannel`·`McpChannel` — **게이트 밖·NotImplementedError**·새 무거운 의존성은 후속 판단). **채널 중립** — 어떤 채널도 1급 아님(포트가 채널 어휘를 안 가정·recipient → 실 주소 변환은 어댑터 안에서). 첫 실 어댑터 후보는 MCP 알림(제품이 MCP 서버라 외부 의존 0)이나 게이트 밖이라 지금 안 정함. **Notification**(frozen 값 객체): `recipient_id`(owner/manager User.id 귀속 키)·`kind`(**NotificationKind** = `Literal["conflict_opened","backup_review_added","reeval_flagged","manager_escalated"]` — 어느 적재가 통지를 낳았나)·`subject_ref`(어느 항목 — case_id/item_id/intent, 멱등 키 일부이자 수신자 손잡이)·`created_at`(주입 clock 결정론). **`kind`는 Literal이지 sealed sum이 아니다** — 네 종류가 *필드 구조 동일·분기 없는 라벨*이라(`ReevalItem.subject_kind`와 같은 판단), sealed sum은 *처분*(행위 선택 — `ReevalOutcome`·`ManagerAction`)에 둔다. **Notifier**(통지 서비스): 발화 지점이 `notify(notification)`만 부르고 Notifier가 구독 맵(`recipient_id → channel`)으로 채널을 찾아 `send`한다. **구독 = 주입 맵**(MVP는 별 `SubscriptionStore` 없이 정적 매핑 — 전이 없는 데이터라 store 불요·동적 구독은 후속 승격). **미구독 recipient는 skip**(처리함 pull 그대로 — 미아 없음). **멱등**: 같은 `(recipient_id, kind, subject_ref)`는 중복 발송 안 함(같은 항목에 발화가 두 번 와도 통지 한 번 — `StalenessPropagator` needs_review 가드·`ManagerQueueStore.get_by_case` 중복 방지와 동형). **분산 인프라(ADR 0011·0012)는 코드 재사용이 아니라 *패턴*(멱등 `ticket_id`·at-least-once 정신)만 따른다** — 통지는 fire-and-forget push라 작업 디스패치(round-trip·claim/submit·큐 상태기계)와 *다른 도메인*이라 `WebSocketDispatcher` 직접 재사용은 과결합(`reeval.py`가 BackupReview 패턴을 *복제*한 정신). **발화 지점**(ADR 0022 결정 4): 각 적재 지점(ConflictCase open·BackupReview add·Reeval add·Manager enqueue)에 `notifier: Notifier | None = None` 옵셔널 주입 — None이면 기존 동작(하위호환·게이트 보존), 비None이면 적재 직후 `notify` 1회(`commit_okf_bundle(..., propagator=None)`과 동형). **단일 발화 추상 = 인터페이스 하나(`Notifier.notify`)이지 함수 하나가 아니다** — 발화 맥락이 제각각(ConflictCase는 ask_org Contested arm, Reeval은 StalenessPropagator)이라 억지 통합(전역 이벤트 버스)은 과추상, 포트 호출 모양만 통일. store는 순수 보관(**전이 ≠ 기록 ≠ 통지** — 통지는 적재 *뒤*의 외부 부작용·전달 로그는 audit과 별 축). **노출 불변식**: 통지는 *운영 면*(owner/manager) 신호라 운영 내부값(case_id·intent) OK이되 **실 사용자 채팅엔 통지 0**(OrgReply는 통지를 모름)·본문에 사용자向 비밀 누설 차단(식별자만·본문 렌더는 어댑터 책임). **발화 지점 4개 전부 구현**(ConflictCase open·Reeval add·Manager enqueue·BackupReview add — 같은 `Notifier.notify` 인터페이스). m1(reeval 두 축 subject_ref 멱등 충돌)은 통지 subject_ref 축 네임스페이스(`precedent:`/`answer:`)로 닫힘. (ADR 0022)
_Avoid_: Webhook(단독 — 실 전송 메커니즘과 혼동), Slack/Email(단독 — 1급 채널 아님·전부 어댑터), Event bus(전역 버스 아님 — 인터페이스만 통일), Subscriber(단독)

**Confidence**:
한 결정·답변의 확신도(0~1). 자기보고값이며 Authority와 무관.

**Trust Label**:
카드·답변의 취급 제약 태그(`internal_only` 등). Authority·Confidence와 구분.

### Eval (골든셋)

**Golden set (골든셋)**:
분류기·라우팅 품질을 검증하는 *라벨링된 평가 데이터*(ADR 0003·TRD §7). 두 출처의 합 — 누적 **Precedent**(운영에서 합의로 떨어진 실 케이스)와 **Sample question**(손으로 라벨링한 시드 질문, `samples/questions.jsonl`). 단위 테스트(결정론)가 *로직 오류*를 매 커밋 잡는다면, 골든셋은 *확률적 LLM 분류·답변 품질*을 정확도/통과율 **임계값 eval**로 잡는다 — 한 예제가 틀리는 게 실패가 아니라 임계 미달·이전 대비 하락이 실패. 같은 골든셋이 두 분류기(`RuleBasedClassifier`·후속 `LlmClassifier`)의 공통 eval 타깃이다. **골든셋 *데이터*(샘플 카드 5장 + 질문 30개)는 T6.4, eval *러너*(정확도 임계값·LLM 연동)는 T6.2** — 데이터와 러너를 분리한다(데이터는 분류기 무관, 러너는 데이터를 소비). T6.4의 자체 게이트는 *결정론*이다(샘플 질문의 `expected_intent`를 `FakeClassifier`로 주입해 라벨↔카드 coherence를 LLM 없이 검증) — 실 LLM 정확도 측정은 T6.2 러너 영역.
_Avoid_: Test set(단독 — 단위 테스트 픽스처와 혼동), Benchmark, Corpus, Dataset(단독)

**LlmClassifier (LLM 분류기)**:
`Classifier` 포트의 LLM 구현(T6.2) — `claude -p` 헤드리스로 질문을 intent로 분류한다. `RuleBasedClassifier`와 *같은 포트*(`classify(question) -> intent`)의 다른 구현이라 Router·골든셋 eval이 공통으로 본다. **전송은 `claude -p` 헤드리스**(`ClaudeCodeRuntime`과 일관 — 중앙 API 키 0·로컬 claude 인증, ADR 0010 정신을 중앙 분류에도). Anthropic API/SDK(중앙 키 필요)는 프로젝트가 회피해 온 방향이라 채택 안 함. `runner`(`ClassifierRunner` Protocol) 주입으로 결정론 경계(`ClaudeCodeRuntime.runner` 패턴 동일 — 실 LLM은 비결정·느리므로 단위 테스트는 FakeRunner). **intent 어휘는 주입**(보통 registry domains 합집합) — LLM이 그 집합에서 하나를 고르거나, 어디에도 안 맞으면 `""`(미분류). 어휘 외 환각·형식오류·미지를 `""`로 정규화하는 게 계약이며(Router가 유효 라벨 또는 ""를 신뢰), `""`는 Router에서 0 매칭 → Unowned로 흘러 미아 없음이 보존된다. 단위 테스트는 runner fake로 결정론(프롬프트에 후보 실림·응답 파싱·미지→""), 실 분류 정확도는 eval(Golden set)로만 본다(ADR 0003). 구현은 mcp-runtime-engineer.
_Avoid_: LlmRuntime(이건 답변 — 분류와 구분), AiClassifier

**Eval runner (eval 러너)**:
골든셋(Golden set) 정확도를 임계값으로 재는 러너(T6.2 — `eval.py`). **두 정확도**를 같은 골든셋 30개에 잰다 — ① **분류 정확도**(`classify(question) == expected_intent` 비율, 분류기를 *직접* 부름 — 라우팅 intent 단일 출처화 후에도 분류 품질은 따로 봐야 하므로 route가 아니라 classify를 잼) · ② **라우팅 정확도**(`route(question)` disposition(+routed면 primary·contested면 candidates)==expected 비율, route 결과로 잼). **한 예제 틀림이 아니라 집계 비율 vs 임계**(예 ≥0.8)가 통과를 가른다(임계 미달·이전 대비 하락이 실패, ADR 0003). **결정론 vs 실 LLM 경계**: 러너 *구조*는 결정론 테스트(주입 분류기가 expected/wrong intent를 내면 정확도 계산·임계 통과/실패가 맞나 — `FakeClassifier`, tdd-engineer)이고, 실 `LlmClassifier`로 도는 정확도 측정은 *게이트 밖*(eval/수동·야간 — 분류기 변경 시·야간 회귀, TRD §7). `EvalReport`(분류·라우팅 정확도·total·threshold·passed)를 산출. CLI 진입점은 구현 자리. 범위 밖: 임베딩 유사도·다중 LLM·프롬프트 튜닝·confusion matrix.
_Avoid_: Test runner(단독 — pytest와 혼동), Benchmark, Scorer

**Sample question (샘플 질문)**:
골든셋의 손라벨링 시드 한 건 — `samples/questions.jsonl`의 한 줄(JSON). `question`(자연어 업무 질문) + 기대 처분 라벨(`expected_intent`·`expected_disposition`(`routed`/`contested`/`unowned`)과 disposition별 부속 `expected_primary`/`expected_candidates`·선택 `expected_approval`/`expected_collaborators`) + `note`(사람용 근거)를 든다. **샘플 질문은 Precedent가 아니다** — Precedent는 운영에서 *합의로* 떨어진 `Resolution` 기록이고(라우터가 lookup), 샘플 질문은 *사람이 미리 박은* eval 기대치다(라우터가 참조하지 않음). 둘 다 골든셋이지만 출처·생애가 다르다(시드 vs 누적). 30개는 세 처분을 다 덮도록 배분된다(Routed/Contested/Unowned). disposition별로 의미 있는 필드만 채운다 — routed면 `expected_primary`, contested면 `expected_candidates`(≥2), unowned면 둘 다 비움(0 매칭). **정적 골든셋(Precedent 0)에서 시연되는 부속은 approval(단일 domain intent의 Routed 게이트)과 cannot_answer(후보 차감 — domains+cannot_answer 양쪽에 든 intent는 후보에서 빠져 Unowned, T6.4가 `router.py`에 구현)뿐** — collaboration은 *그 intent가 2장 domains에 있어야 부착*되는데 그러면 후보 ≥2라 Contested가 되어 Routed가 안 나오므로(Precedent가 단일 primary로 고정해야 살아남), `expected_collaborators`는 스키마에만 두고 시드엔 비운다.
_Avoid_: Test case(단독 — 단위 테스트와 혼동), Fixture, Precedent(이건 운영 누적 — 샘플 질문은 시드 라벨)

### Audit (운영자向 기록)

**Audit log (감사 로그)**:
운영자向 append-only JSONL 기록. 한 질문 처리의 전체 절차(질문·intent·라우팅 처분·디스패치 결말)를 *내부값까지* 담는다 — OrgReply(사용자向)가 감춘 `confidence`·`candidates`·`escalated_to`·`primary`, 그리고 `Pending(dispatched)`가 떨군 디스패치 escalation의 `manager_id`·`reason`까지 여기선 전부 기록. **검토 전이(`BackupReview`)는 audit에 남기지 않는다** — 검토 기록은 `BackupReviewStore.history`(전이 보관소)가 담당하고 audit은 질문→라우팅→디스패치→답의 절차 전용이다(ADR 0012 결정 7 확정). **모니터링 면(T5.1)의 데이터 원천** — 운영자가 "모든 질문의 절차·답"을 보는 화면(PRD §7 시나리오 5)이 이 로그를 *순수 읽기*로 투영한다(새 도메인 상태·전이 0). 전이가 아니라 기록(전이 ≠ 기록).
_Avoid_: Trace(단독 — 사용자에게 감추는 라우팅 내부와 혼동), Log(단독)

**AuditReader (감사 읽기 포트)**:
운영 모니터링 면(T5.1)이 감사 로그를 *순수 읽기*로 보는 포트 — `AuditLog`(쓰기 전용 `record`)와 **인터페이스를 분리**한다(ISP). 쓰는 주체(`ask_org`, 매 질문)와 읽는 주체(web/모니터링 면)가 다르기 때문이다. `PrecedentStore`(record+lookup을 한 포트에 둠)와 갈리는 지점이 바로 이 *주체 분리*다 — 거긴 라우터 한 컴포넌트가 record·lookup을 둘 다 자연스럽게 쓰지만, audit은 쓰기·읽기 주체가 갈려 ISP가 맞다. 두 구현체(**JsonlAuditLog**·**InMemoryAuditLog**)가 record(쓰기)와 records·record_at(읽기)를 *둘 다* 구현해, `ask_org`는 `AuditLog`만, 모니터링은 `AuditReader`만 의존한다. 메서드: `records() -> list[dict]`(기록 순서 전체 — 목록 요약의 원천, 비면 빈 리스트=경계) · `record_at(index) -> dict | None`(상세 보기의 원천, 범위 밖이면 None). **반환 단위 = 직렬화된 레코드(dict, `AuditEntry.as_record()` 모양)**이지 `AuditEntry` 객체가 아니다 — `JsonlAuditLog`는 파일을 되읽는데 JSONL→`AuditEntry` 역직렬화가 손실(직렬화는 `agent_id`만 남기고 `AgentCard` 본문·`RoutingDecision`/`DispatchOutcome`을 재구성 못 함)이라 *기록된 줄 자체*가 모니터링 단위다(두 구현체가 같은 dict 모양을 줘 균일한 계약). **주소 지정 = 인덱스**(0-based, 기록 순서) — append-only라 인덱스가 안정적이다(새 항목은 끝에 append, 기존 위치 불변·삭제 없음). `entry_id`(uuid)는 결정론을 깨거나 주입 부담이 커 MVP엔 과하다. 모니터링은 *운영 면*이라 내부값 노출 OK(채팅 OrgReply 불변식의 반대 — Inbox·Manager 큐와 같은 면). Org 그래프 조회는 별 면(`Org graph view`, T5.3 — 같은 운영 면이되 감사 로그가 아니라 레지스트리 파생)이다. 검색·필터·페이지네이션·실시간 푸시는 범위 밖(자리만).
_Avoid_: AuditQuery, MonitorStore, LogReader(단독)

**AuditEntry**:
Audit log의 한 줄. 한 질문 처리 절차의 기록 단위 — `timestamp`·`user_id`·`question`·`intent`·`decision`(RoutingDecision 원형, 내부 상세 보존)·`dispatch_outcome`(DispatchOutcome 원형, Routed일 때만; Contested/Unowned는 디스패치를 안 하므로 `None`). `intent` 필드는 질문 처리의 1급 기록이라 명시 필드로 유지하되, **그 값의 출처는 `decision.intent` 하나다(ADR 0015)** — ask_org가 자기 classify가 아니라 라우팅 결정이 실은 intent를 넘겨, 기록된 intent가 라우팅이 실제로 본 intent와 항상 같다(divergence 차단). `decision`도 같은 intent를 들지만 audit의 명시 `intent` 필드는 기록 단위로 더 읽혀 파생 프로퍼티로 접지 않는다(`answer`는 dispatch_outcome 파생인 것과 구분 — intent는 라우팅·디스패치 모든 처분에 공통이라 1급 유지). OrgReply가 decision·outcome을 투영해 버리는 것과 달리 **둘 다 원형을 그대로 안는다** — 한 질문 처리의 두 절차(라우팅→`decision`, 디스패치→`dispatch_outcome`)를 1급으로 기록. `EscalatedToManager`의 `manager_id`·`reason`은 사용자向 `Pending`에선 떨궈지지만 여기선 전부 남아, `Unowned.escalated_to`를 남기는 것과 *대칭*을 이룬다(둘 다 "escalation 대상" — 같은 처분이 기록 차원에서 같은 모양). `answer`는 별도 필드가 아니라 `dispatch_outcome`에서 유도하는 파생 접근자다(`Delivered.answer`만 답을 가짐 — 같은 답을 두 곳에 두지 않기 위함, SSOT는 `dispatch_outcome`).

### Authn & planes (운영 면 인증)

**Operational plane (운영 면)**:
실 사용자 채팅(`/ask`·index.html)과 *구분되는* 운영자/Owner/Manager의 화면 — Owner 처리함(`/inbox*`)·Manager 큐(`/manager*`)·운영 모니터링(`/monitor*`). ADR 0009가 "인증으로 분리된 별개 공간"으로 못박은 그 면이다. **운영 면은 로그인(세션 신원)을 요구**하고 채팅은 익명 유지(다른 공간) — 한 세션이 운영 신원 1개로 고정돼 *페르소나 혼입*(한 화면에서 여러 면 임의 전환)을 막는다. 운영 면은 내부값 노출 OK(인증된 운영자가 봄 — 채팅 OrgReply 불변식과 다른 면). (ADR 0009·0016)
_Avoid_: Admin panel(단독), Dashboard, Backoffice

**Operator session (운영 세션)**:
운영 면 진입에 요구되는 *세션 신원* — 서명 쿠키에 담긴 `user_id` 하나(ADR 0016). 세션 키에 박히는 `user_id`를 *어떻게 얻느냐*가 인증 모드를 가른다(아래 SSO 인증 모드). `POST /login`(body `user_id`, **Registry에 실재하는 User여야** — 없으면 401)이 무비밀번호로 세션을 set하고 `POST /logout`이 클리어. 메커니즘은 starlette `SessionMiddleware`(`itsdangerous` 서명, secret는 env/주입·커밋 금지)이되, *읽는 코드는 헬퍼 한 곳*(`_session_identity`)으로 격리해 메커니즘 교체에 엔드포인트가 안 흔들린다(헥사고날). **신원 출처 이동(핵심 보안)**: 운영 엔드포인트의 1인칭(`by_owner`·`by_manager`)·귀속 키(`owner_id`·`manager_id`)는 *path/body가 아니라 세션*에서 온다 — body·path는 신원 출처가 아니다(위조 차단). 도메인 1인칭 강제(ConsensusService·BackupReviewService·ManagerQueueService의 ValueError)는 그대로이되 그 값의 *출처가 세션*이라 가장이 불가능. **재정의(T7.1·ADR 0021 — ADR 0016 폐기 아님)**: ADR 0016이 "SSO는 v0 범위 밖, 무비밀번호 *신원 선택*"이라 한 부분은 SSO 모드에서 "IdP가 *증명*한 신원"으로 *재정의*된다. 무비밀번호 `/login`은 사라지지 않고 *OFF/하위호환 모드*로 보존되며, SSO를 켜면(아래) `/login`은 403으로 막히고 `/login/sso`(증명)만 산다. 신원 출처가 세션으로 이미 격리돼 있어(`_session_identity`) *세션에 박기 직전의 검증*만 선택→증명으로 바뀐다 — 세션을 읽는 모든 코드(스코프·concur·빌더 OKF 커밋 author)는 무변경. (ADR 0016·0021)
_Avoid_: Login(단독 — 행위와 상태 혼동), Auth token, Credential, JWT, Persona(이건 PRD 화면 주체 — 세션은 그 신원 고정 메커니즘)

**SSO 인증 모드 (3단 — OFF · 무비밀번호 · SSO)**:
운영 면 인증의 점진 전환 3단(T7.1·ADR 0021 결정 4). 세션 키에 박는 `user_id`를 얻는 채널이 모드를 가른다 — ① **OFF**(`session_secret` 없음): 세션 미부착·데모·하위호환. ② **무비밀번호**(`session_secret`만): `POST /login`이 *신원 선택*(ADR 0016 기존 동작 보존). ③ **SSO**(`session_secret` + `oidc_provider`): `POST /login/sso`(body `{id_token}`)가 *신원 증명*만 세션에 박고, 무비밀번호 `POST /login`은 **403 거부**(신원 선택 우회 차단 — 증명 채널을 켜면 선택 채널을 닫아야 owner 종속이 실재). `create_app`·`create_central_app`에 `oidc_provider: OidcProvider | None = None` 주입(미주입이면 기존 동작 — 하위호환). 이것이 owner 종속(card.owner)의 *증명* 미싱피스를 채운다(ADR 0017 결정 5 — 선언[card.owner 중앙]·강제[편집 스코프 403]·증명[신원=SSO] 셋 중 증명). single tenant 사내라 tenant 개념·격리·프리픽스 없음. (ADR 0021·0017 결정 5)
_Avoid_: OAuth flow(단독 — 모드 구분 아님), Multi-tenant(single tenant), RBAC(SSO는 신원 증명이지 역할 부여 아님 — 역할은 그래프 파생)

**OidcProvider (OIDC 검증 포트)**:
id_token을 검증해 신뢰할 수 있는 claim으로 바꾸는 *최소 포트*(ADR 0021 결정 1·`oidc.py`) — `verify(id_token) -> OidcClaims`(서명·만료·aud 검증·실패 시 `OidcVerificationError`). 신원 *검증*은 비결정·외부 의존(IdP·JWKS·서명)이라 `GitGateway`·`AgentRuntime`·`ClaudeRunner`와 **같은 포트 패턴**으로 격리: 실 구현 `HttpOidcProvider`(JWKS fetch·RS256 서명 검증·iss/aud/exp — **게이트 밖 수동 시연**, 새 무거운 의존성 추가는 후속 판단)와 결정론 `FakeOidcProvider`(in-memory 토큰→claims 맵·위조/만료 거부 시뮬 — 단위 테스트 주입). **공급자 중립** — 표준 OIDC claim(sub·email·email_verified·iss·aud)만 가정한다(공급자별 특수 claim Google `hd`·MS `tid`는 안 씀 → 어떤 OIDC IdP에도 붙는다). (ADR 0021)
_Avoid_: AuthProvider(단독 — 너무 넓음), IdpClient, OAuthClient, JwtVerifier(JWT는 한 표현·포트는 OIDC 검증)

**OidcClaims (OIDC claim 값 객체)**:
검증을 통과한 신뢰할 수 있는 OIDC claim의 *frozen 도메인 값 객체*(ADR 0021 결정 2·`oidc.py`) — `sub`·`email`·`email_verified: bool`·`iss`·`aud`. `verify`가 서명·만료·aud를 검증한 *뒤*의 신원이라, 이 값 객체가 존재한다는 것 자체가 "유효성 검증 통과"를 뜻한다. **`exp`는 일부러 안 든다** — 만료는 *검증의 책임*(verify가 만료 토큰을 OidcVerificationError로 걸러냄)이지 검증 통과 후의 도메인 값이 아니다(`Answer`가 snapshot_sha는 들되 검증용 메타는 안 드는 경계와 동형). 공급자 중립이라 특수 claim 없음. frozen 값 객체이지 전송 와이어 DTO(`SsoLoginRequest{id_token}`)가 아니다. (ADR 0021)
_Avoid_: TokenPayload(전송 DTO 연상 — 도메인 값 객체임), Identity(단독 — 너무 넓음·resolve_identity 결과 user_id와 혼동), JwtClaims

**resolve_identity (신원 매핑 — 순수 함수)**:
검증된 claim을 registry user_id로 매핑하는 *순수 함수*(ADR 0021 결정 3·`oidc.py`) — `resolve_identity(claims, registry) -> str`. web과 분리한 경계(`validate_card_for_builder`·`serialize_reply`와 같은 결 — 결정론 테스트). **baseline = verified email 매핑**: ① `claims.email_verified`가 True가 아니면 거부(미검증 이메일은 신원으로 못 씀) ② `claims.email == user.email`인 User를 registry에서 찾는다(**User.email이 SSOT** — `User`에 `email: str | None = None` 신설·frozen·하위호환·admission 무관) ③ 정확히 1명이면 그 user_id, 0매칭/모호면 거부(`OidcVerificationError` — 증명은 됐으나 우리 조직 신원으로 못 잇는 경우, web이 401로). email local-part==user_id(회사 이메일 규칙 종속·기각)·외부 매핑 테이블(SSOT 이중화·기각) 대신 User.email 한 곳에 신원을 모은다. single tenant라 tenant 스코프 없음(registry 전체에서 email로 찾음). (ADR 0021 결정 3)
_Avoid_: map_user(단독 — 너무 일반), lookup_identity, authenticate(인증은 verify·이건 매핑)

**Operational scope (운영 스코프 — 자기 것만)**:
세션 신원이 *자기에게 귀속된* 대상만 보고 처리하는 경계 — Owner는 자기 소유 카드의 처리함만(ADR 0009 기준 2), Manager는 자기 큐만. **구조적 강제**: 자기 면 조회는 path param을 *제거*해(`/inbox/cases`·`/inbox/backup-reviews`·`/manager/queue` — 세션 신원으로) "남의 것을 path로 지목"할 표면 자체를 없앴다. 1인칭 처분(concur·review·manager act)은 세션 신원이 그 대상의 owner/manager가 아니면 거부 — 도메인 ValueError를 **403**(자기 권한 밖)으로 매핑. 실패 코드: 미로그인 **401** · 스코프 위반 **403** · 대상 미존재 **404** · 입력 형식 오류 **400**. **역할은 그래프에서 파생**(별 role DB·RBAC 매트릭스 없음) — owns면 Owner면·자기 manager_id 큐면 Manager면. 모니터링(`/monitor*`)은 세분 역할 없이 *인증만* 요구(로그인하면 봄, 운영자 역할 분리는 후속). 깊은 RBAC·권한 매트릭스·CSRF·rate limit은 범위 밖. (ADR 0009·0016)
_Avoid_: RBAC(이건 깊은 역할 — v0는 그래프 파생 스코프), Permission matrix, ACL, Authorization(단독 — 자기 것만의 좁은 강제)

### Operator surfaces (운영 면 조회·구성)

**Org graph view (Org 그래프 조회)**:
운영자가 "전체 그림"(PRD §4)을 보는 운영 면 — User·Agent Card 2노드 그래프(Graph model 절)를 *레지스트리에서 순수 파생*해 노드·엣지로 투영한다. **새 도메인 상태·전이 0** — Registry(이미 admission으로 무결성 보증된 진실)를 읽어 그릴 뿐, 그래프는 별도 store가 아니다(모니터링이 감사 로그를 순수 읽기로 투영하는 것과 같은 결, 원천만 audit→registry). 투영 단위: **노드** = User(`{type:"user", id, manager?}`) + Agent Card(`{type:"card", agent_id, owner, team, domains, maintainer?}`), **엣지** = `owns`(owner User→card) · `manages`(user.manager→user) · `maintains`(maintainer User→card, **카드에 `maintainer`가 있을 때만** — MVP는 maintains 엣지 없으면 owns가 대신이라 대개 owns만, ADR 0005). 직렬화는 순수 함수 `serialize_org_graph(registry) -> {"nodes": [...], "edges": [...]}`(web과 분리 — `serialize_reply`·`summarize_audit_record`와 같은 경계)로 격리해 결정론 테스트한다. 운영 면이라 내부값(domains 등) 노출 OK(채팅 OrgReply 불변식의 반대 — 모니터링·Inbox·Manager 큐와 같은 면). 인증은 *모니터링과 동일*(세분 역할 없이 인증만 — 로그인하면 봄, ADR 0016 결정 5). force-layout 그래프 라이브러리·드래그 편집·실시간 갱신은 범위 밖(정적 SVG 수동 시연). (PRD §4·ADR 0005)
_Avoid_: Org chart(단독 — 사람 위계만 연상, 여긴 User+Card 2노드), Topology, Network diagram, Graph store(별 store 아님 — 레지스트리 파생)

**Card composer (Agent 빌더 — 카드 구성·검증)**:
개인 Owner가 "자기 에이전트를 깎는"(PRD §4) 운영 면 — 카드 필드를 폼으로 구성 → **admission 검증**(유효하지 않은 카드는 등록 안 됨, PRD §10) → 통과면 **YAML 미리보기**를 낸다. **편집 채널은 빌더가 아니라 git/PR이다**(Maintainer 절 — "편집 권한·이력은 git/PR이 강제·기록"): 빌더는 라이브 레지스트리에 *쓰지 않고*(라이브 mutation은 git/PR-as-SSOT와 충돌·비영속이라 기각), 검증 통과 카드의 `registry/agents/{agent_id}.yaml` 텍스트를 보여 Owner가 복사→커밋(PR)한다 — 그래야 admission 불변식을 *인터랙티브로 시연*하면서 카드 편집 채널은 git/PR 하나로 유지된다(편집 채널 이중화·드리프트 차단). **새 도메인 상태·전이 0**(기존 `AgentCard`·`Registry.validate` 정신 재사용 — 새 타입 안 만듦). 검증 경계: `POST /builder/validate`(카드 필드 JSON → `AgentCard.model_validate`[필수 필드·타입] + admission 규칙[owner 실재·참조 무결성, `Registry.validate`와 같은 검사]) → 통과면 YAML, 실패면 사유. **Owner 스코프(ADR 0016 정합)**: 인증 ON이면 세션 신원이 카드 `owner`와 달라야 거부(자기 카드만 깎음 — 운영 스코프), 미로그인 401, 인증 OFF(데모)는 자유 구성. 실 파일 쓰기·git 조작·카드 버저닝 UI·드래그 편집은 범위 밖(연결점만 — git/PR 채널). (PRD §4·ADR 0005·0016)
_Avoid_: Card editor(단독 — 라이브 편집 연상, 여긴 구성·검증·YAML 출력이고 편집은 git/PR), Form builder, Registry writer(라이브 쓰기 아님), Wizard

**OKF Builder (빌더 OKF 편집 면)**:
빌더 UI의 *두 번째 면* — 개인 Owner가 자기 **OKF 번들(답변 지식)**을 폼/에디터로 고치면 **빌더가 owner 대신 git 커밋**한다(ADR 0018 결정 1·5·T7.2 — owner는 git을 몰라도 됨). Card composer(카드 면)와 *분리*다: 카드 면은 라우팅 메타를 검증→YAML→수동 PR(admission 경계라 자동 커밋 안 함), OKF 면은 답변 지식 마크다운을 *자동 커밋*한다(잦은 갱신·admission 경계 아님이라 안전 — "살아있는 지식"의 본체, ADR 0017 결정 3). 한 번 편집 = 한 번들(`agent_id`)에 파일 N개 쓰기 + 커밋 1개(가장 작은 닫힌 루프). 커밋 author=owner 신원(세션 신원 — ADR 0016 위조 차단, T7.1 SSO 전), 편집 스코프는 *카드 면과 같은 Owner 스코프*(세션 신원 ≠ `card.owner` → 403, web 경계 강제). 입력은 `BuilderCommitRequest`(web과 분리한 순수 입력 — `BuilderValidateRequest`와 같은 결), 오케스트레이션은 `commit_okf_bundle(req, gateway)`(web과 분리한 순수 함수 — `validate_card_for_builder`와 같은 경계, `GitGateway` 주입 결정론). 편집 채널은 *여전히 git 하나*다(빌더가 *그 채널로 커밋*할 뿐 — 라이브 레지스트리 mutation 이중화 아님, Maintainer 절·ADR 0013 안 B 정신). 실 git(`SubprocessGitGateway` — T8.1 a·b)·OKF 에디터 UI(빌더 OKF 편집 탭 — T8.1 d)는 실체화됨. 원격 repo·중앙 최신 읽기(pull/webhook 캐시)는 후속(외부 결정). (ADR 0018·0017 결정 3)
_Avoid_: Knowledge editor(라이브 편집 연상 — 커밋 채널임), Wiki, CMS, Direct write

**GitGateway**:
OKF 번들 git 저장·빌더 커밋·커밋 스냅샷 추출의 *최소 포트*(ADR 0018 결정 3) — `commit_bundle`(owner author로 파일 쓰기+커밋 1개→SHA)·`head_sha`(repo 최신 커밋)·`extract_snapshot`(`git archive <sha>`로 그 커밋 트리를 읽기전용 임시 디렉터리로 추출→cwd). 실 git은 *비결정·부작용*이라 `ClaudeRunner`·`AgentRuntime`과 **같은 포트 패턴**으로 격리: 실 구현 `SubprocessGitGateway`(`git` CLI subprocess — T8.1 (a)(b)로 구현, *tmp repo 통합 테스트*로 게이트에 들임; 실 SHA 값은 시각/환경 의존이라 행위 단언만·git 없으면 skip; 새 의존성 0·subprocess·tarfile만·GitPython 안 씀; committer는 author=owner와 분리해 `agent-org-builder` 고정)와 결정론 `FakeGitGateway`(in-memory 커밋 로그·결정 SHA — 단위 테스트 주입). 경로 탈출 거부는 모듈 함수 `validate_okf_paths`(`OkfFile.path`)·`validate_agent_id`(`agent_id`)로 두 구현이 *같은 규칙*을 공유(안전 경계 단일화). `validate_agent_id`는 admission(ADR 0023 — `AgentCard`가 형식 권위)의 *경로 안전 백스톱*이다 — admission이 상류에서 형식을 강제해도 어댑터는 카드를 안 거치는 경로(`extract_snapshot`의 인자 `agent_id` 등)를 막는 심층 방어로 *유지*한다(역할 분리: admission=positive format·어댑터=traversal block). 커밋 스냅샷 실행은 *working tree 직독이 아니라* archive 추출본을 읽어(ADR 0018 결정 4) "이 답은 이 커밋 기준"을 재현(읽은 내용 = 그 SHA 트리·동시 읽기 충돌 0·`Answer.snapshot_sha`로 감사). `okf_root`의 의미가 ADR 0018에서 "OKF 번들들을 담은 *git repo 작업 트리 루트*"로 정밀화된다(T6.7 working tree 직독과 커밋 스냅샷 실행을 양립). (ADR 0018)
_Avoid_: Git client, Repo manager, VCS(추상 과함 — 우리는 최소 4연산 포트), GitPython

## Flagged ambiguities

**"Agent" 단독 사용 금지**: 도메인 모델·코드·이 문서에서 맨 단어 "Agent"는 쓰지 않는다 — 항상 **Owner / Agent Card / Agent Runtime** 중 하나로 한정. (제품·마케팅 산문에서는 "에이전트" 자유 사용 OK.)

## Example dialogue

— "이 질문 누구한테 보내야 해?"
— "Router가 등록된 Agent Card들이랑 대조해서 담당 후보를 골라. 카드 자체는 Registry에 있고."
— "그 카드 누가 관리하는데?"
— "각 Owner가 자기 카드만 관리해. 중앙은 카드 내용을 직접 소유하지 않아."
