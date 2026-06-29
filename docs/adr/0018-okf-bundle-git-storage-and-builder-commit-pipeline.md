# OKF 번들을 git에 저장하고 빌더 UI가 owner 대신 커밋한다 — 답 실행은 커밋 스냅샷을 cwd로 읽는다

상태: accepted (2026-06-21) · **구현 완료(T7.2 슬라이스 1~4 — 결정론 게이트; T8.1 (a)(b) — `SubprocessGitGateway` 실 git CLI 어댑터 3메서드 + tmp repo 통합 테스트 27개, 게이트 795 passed/pyright 0/ruff 0; 슬라이스 5의 (c) 실 claude end-to-end·(d) OKF 에디터 UI·(e) 중앙 최신 읽기 pull/webhook은 후속)** · **ADR 0017 결정 3의 구체화**(살아있는 지식 = git 저장 + 빌더 UI 편집 + 커밋 스냅샷 실행) · ADR 0013(OKF·카드/번들 분리·cwd 소비)·ADR 0012(`DelegationSnapshot.snapshot_at` 신선도)와 정합 · T6.7(`ClaudeCodeRuntime` OKF cwd 소비)의 *저장·편집·스냅샷 축*을 닫는다. · **ADR 0029(OKF 자동 저작)가 이 위에 "자동 초안 채우기"를 얹음**(커밋 메커니즘[`GitGateway`·`commit_okf_bundle`·author=owner·스코프 card.owner] 무변경·편집 대상[OKF 마크다운]을 LLM 초안으로 미리 채우는 입력 자동 생성만 추가·커밋 본체가 OKF 번들이지 카드 YAML 아닌 것[결정 1]·`OkfChangeEvent` 발화[결정 6]가 증분 재인덱싱 트리거인 것 보존). · **ADR 0030(owner측 저작 토폴로지)이 이 ADR을 *재해석***(supersede 아님·확장): OKF git을 *owner-로컬 repo*로 re-home(결정 2의 "owner별 repo 후속 옵션" 실체화·중앙 모노repo 아님), **카드 빌더(라우팅 메타)=중앙 유지**(결정 1·`web.py`)/**OKF 저작면(raw→LLM 초안→diff 검토)=owner측 신규**의 분할을 명시, 커밋 메커니즘·author=owner·스코프·`OkfChangeEvent` 발화는 무변경(이 ADR 결정 전부 보존).

## 맥락 — 끊어진 고리 둘

T6.7(ADR 0013)은 OKF 번들을 *워커가 cwd로 읽어 답한다*까지 닫았다. 하지만 그 번들이 **어떻게 git에 들어가고**(저장), **비개발자 owner가 어떻게 편집하며**(편집 UX), **답이 어느 시점 스냅샷을 읽는가**(실행)는 비어 있다. ADR 0017 결정 3이 방향을 줬다 — "OKF를 git에 두고(버전·리뷰·감사 공짜), **빌더 UI가 owner 대신 커밋**(owner는 git 몰라도 됨), 실행은 *커밋 스냅샷*(워크트리 X)". 본 ADR이 그 세 축을 도메인·아키텍처로 확정한다.

현재 상태의 두 고리가 끊어져 있다:

1. **저장·편집** — 빌더(T5.3)는 *카드 필드*를 받아 검증→카드 YAML 미리보기까지만 한다. owner가 YAML을 *손으로 복사→git 커밋*해야 한다(비개발자엔 마찰). git 관련 코드·의존성은 0. OKF 번들(`okf/{agent_id}/`)은 손으로 만든 샘플뿐 — 빌더로 만들거나 고칠 길이 없다.
2. **실행 시점** — 런타임(T6.7)은 `okf_root/{agent_id}/`를 *working tree 그대로* cwd로 읽는다. "이 답은 이 커밋 기준"이라는 *재현 가능한 스냅샷 감사*가 없다 — working tree는 커밋 사이에 떠 있어, 답이 어느 지식 상태로 만들어졌는지 못 박는다.

## 결정

### 1. 빌더가 커밋하는 본체 = **OKF 번들 마크다운**이다 (카드 YAML 아님 — 분리 유지)

가장 결정적 모호성. ADR 0017/T7.2 텍스트의 "OKF를 git에 두고 빌더가 커밋"은 **OKF 번들(답변 지식)**을 가리킨다 — 카드(라우팅 메타)가 아니다(ADR 0013 안 B: 카드 ≠ 번들). 두 편집 대상을 *서로 다른 채널*로 가른다:

| 편집 대상 | 무엇 | 채널 | 자동 커밋? | 근거 |
|---|---|---|:---:|---|
| **OKF 번들**(마크다운 + 프론트매터) | owner 통제 *답변 지식* — 잦은 업데이트 | **빌더 UI → 빌더가 owner 대신 커밋**(이 ADR) | **⭕** | "살아있는 지식"의 본체·잦은 갱신·admission 경계 아님 |
| **AgentCard**(라우팅 메타 YAML) | `domains`·`can_answer`·admission 단위 | 기존 빌더 검증 → **YAML 미리보기 → 수동 PR**(T5.3 그대로) | ✕ | 등록 무결성 경계 — 라이브/자동 mutation이면 admission이 흔들림(ADR 0004·0013 안 B) |

**왜 OKF가 자동 커밋의 1순위인가.** ADR 0017 결정 3이 "빌더가 owner 대신 커밋"을 부른 *동기*는 **비개발자 owner의 잦은 지식 업데이트**다. 잦게 바뀌는 것은 지식(환불 정책 금액, 표준 약관 문구)이지 라우팅 메타(담당 도메인)가 아니다. 그리고 OKF 번들은 admission 불변식의 *경계가 아니다*(중앙은 번들 내용을 검증하지 않는다 — 마크다운은 자유, ADR 0013) — 그래서 *안전하게 자동 커밋*할 수 있다. 반대로 카드는 등록 무결성 경계라(유효하지 않은 카드는 등록 안 됨), 자동 커밋이 그 경계를 우회할 위험이 있어 *검증→YAML→PR*을 유지한다.

**그래서 MVP 한 바퀴 = OKF 번들 편집·커밋·스냅샷 실행.** 카드 빌더는 손대지 않는다(검증→YAML 미리보기 그대로). 빌더 UI에 *OKF 번들 편집 면*을 하나 더한다(카드 면과 분리). 한 owner의 한 번 편집 = 그 owner의 번들에 *마크다운 파일 1개 쓰기 + 커밋 1개* — 가장 작은 닫힌 루프.

### 2. 저장소 구조 = 모노repo 하위폴더 `okf/{agent_id}/`, owner별 repo는 후속 옵션

현재 패턴(`okf/{agent_id}/`, working tree)을 MVP 저장 구조로 *그대로 채택*한다. `okf_root`가 가리키는 디렉터리가 **하나의 git repo**(또는 그 일부)라고 가정한다 — MVP에선 **이 프로젝트 repo 자체**(`okf/`가 그 하위폴더)다.

- **MVP = 모노repo + 경로 규약**(`okf/{agent_id}/`). owner 범위 쓰기는 *CODEOWNERS*(`okf/{agent_id}/ @owner` — 후속 T7.1 SSO와 연결)로 표현 가능하나 MVP 강제는 아니다. agent_id가 번들 경로를 진다(ADR 0013 안 B 그대로 — 레이블 아닌 agent_id).
- **owner별 repo(기각 — 지금은)**: owner마다 격리 repo는 진짜 데이터 격리(중앙이 읽으면 안 되는 케이스, ADR 0017 옵션 B)엔 맞지만, 기본 경로(중앙이 OKF 최신을 *읽는다*)엔 과하다 — repo N개의 클론·동기화·권한 운영이 추가된다. 모노repo가 *커밋·스냅샷·CODEOWNERS*를 다 주면서 가볍다. (격리가 진짜 요구인 팀은 owner별 repo로 — 후속.)

`okf_root`의 의미를 *"OKF 번들들을 담은 git repo 작업 트리 루트"*로 못 박는다(T6.7의 "owner 환경 루트"를 git repo 루트로 정밀화). 빌더 커밋·스냅샷 실행은 이 repo를 대상으로 한다.

### 3. 커밋 메커니즘 = `GitGateway` 포트로 추상, Fake 주입 결정론 / 실 git subprocess는 게이트 밖

실 git은 *비결정·부작용(파일 쓰기·커밋 SHA 생성)*이라, `ClaudeRunner`·`AgentRuntime`과 **같은 포트 패턴**으로 격리한다:

- **`GitGateway` 포트(Protocol)** — 빌더가 OKF 파일을 쓰고 커밋하는 *최소* 연산만 노출. 실 구현(`SubprocessGitGateway`)은 `git` CLI를 subprocess로 부르고(부작용·게이트 밖 수동 시연), 단위 테스트는 `FakeGitGateway`(in-memory 커밋 로그·결정 SHA)를 주입한다.
- **새 의존성 0 — subprocess만.** GitPython 등 라이브러리를 *더하지 않는다*(과도한 의존 회피). 우리가 쓰는 git 연산은 `add`·`commit`·`rev-parse`·`archive`(아래 4)뿐이라 subprocess가 가볍다. `ClaudeCodeRuntime`이 `claude`를 subprocess로 부르는 것과 같은 결.
- 빌더 커밋 *오케스트레이션*(파일 쓰기 → 커밋 → SHA 회수)은 web과 분리한 **순수 함수/서비스**(`validate_card_for_builder`가 web과 분리된 것과 같은 경계)로 둬, `FakeGitGateway` 주입으로 결정론 테스트한다.

### 4. 커밋 스냅샷 실행 = `git archive` 추출 cwd, 답에 커밋 SHA 감사 메타

"워크트리 없이 특정 커밋을 cwd로" = **`git archive <sha> | tar -x`로 그 커밋 트리를 *읽기전용 임시 디렉터리*에 추출**해 그 디렉터리를 `claude -p`의 cwd로 준다. working tree 직독이 아니다 — 추출본은 *그 커밋의 정확한 스냅샷*이라 "이 답은 이 커밋 기준"이 재현된다.

- **왜 working tree 직독+SHA 기록(기각)이 아닌가**: working tree는 커밋 사이에 떠 있어(미커밋 변경·다른 브랜치 체크아웃) "SHA만 기록"하면 *기록한 SHA와 실제 읽은 내용이 어긋날* 수 있다. archive 추출은 그 어긋남을 원천 차단한다(읽은 내용 = 그 SHA의 트리). 여러 답이 동시에 다른 커밋을 읽어도 충돌 없다(추출본은 독립). ADR 0017 결정 3의 "워크트리 불필요·여러 브랜치 동시 체크아웃 문제 없음"을 archive가 실현한다.
- **MVP 단순화**: 어느 커밋을 읽나 = *HEAD(최신 커밋)*. "중앙 최신 읽기 = pull/webhook 캐시"는 후속 슬라이스로 분리(ADR 0017 결정 3 괄호). MVP는 *로컬 repo의 HEAD*를 스냅샷한다(빌더가 방금 커밋한 그 HEAD).
- **감사 메타 = `Answer.snapshot_sha`**(신규, optional). 답이 *어느 커밋의 OKF로 만들어졌나*를 `Answer`에 실어("이 답은 이 커밋 기준"), 모니터링·감사 로그가 본다. `mode`·`sources`와 같은 *답에 붙는 신뢰/출처 메타*의 연장이다(노출 불변식 — 운영 면 노출 OK, 채팅 노출은 sources 정신으로 후속 판단). audit의 답 직렬화(`_answer_record`)가 `snapshot_sha`를 (None 아닐 때) 함께 실어 기록한다 — `Answer`가 진실 원천이고 별도 상태로 또 박지 않는다(구현 M1, T7.2). 채팅 OrgReply엔 미노출(`Answered`에 필드 없음 — 노출 불변식).

`bundle_dir(card)`(T6.7, working tree 직독)와 **새 스냅샷 경로를 양립**시킨다 — `okf_root`만 주면 기존 working tree 직독(하위호환), *커밋 스냅샷 모드*를 켜면(예: `GitGateway` 주입) archive 추출 cwd. MVP는 둘 다 지원하되 스냅샷 모드가 ADR 0017 정합 경로다.

### 5. owner 귀속 = 커밋 author = 세션 신원, 편집 스코프는 기존 Owner 스코프 재사용

빌더 OKF 커밋의 author·committer를 **owner 신원**으로 박는다(거버넌스 — "신원·책임 귀속" ADR 0017 결정 1·5).

- **신원 출처 = 세션**(ADR 0016 — path/body 아님, 위조 차단). T7.1 SSO 전이라 MVP는 *세션 신원*(`_session_identity`)이 곧 owner다. SSO가 붙으면 회사 신원(`user_id`)이 그 자리에 그대로 들어간다(자리만 열어둠).
- **편집 스코프 = 기존 Owner 스코프 재사용**(ADR 0016): 세션 신원 ≠ 편집 대상 카드의 `card.owner` → **403**(빌더 카드 검증의 스코프와 *같은 규칙*). 자기 번들만 편집·커밋한다.
- 커밋 메시지·author에 owner를 실어, git이 *누가 무엇을 언제 바꿨나*를 공짜로 기록한다(감사 = git, ADR 0017 "버전·리뷰·감사는 git이").

### 6. 신선도 메타 연결점 = 커밋 SHA ↔ `snapshot_at`/`last_reviewed_at`, 본체는 T7.3

신선도(ADR 0017 결정 3·ADR 0012)와 커밋의 관계는 *자리만* 연다(본체는 T7.3 변경 전파):

- 빌더 OKF 커밋은 *그 번들이 방금 갱신됐다*는 사건이다 — 그 커밋 시각이 `last_reviewed_at`(신뢰 신호)·`snapshot_at`(staleness 기준)의 *갱신 트리거*가 될 수 있다(연결점). MVP는 커밋과 신선도 필드를 *자동 연동하지 않는다* — 커밋 SHA를 `Answer.snapshot_sha`로 노출하는 데까지.
- T7.3(변경 전파)이 "정책이 바뀌면(=OKF 커밋) 그 정책에 기댄 Precedent·답을 재검토"를 구현할 때, *그 커밋이 변경 이벤트*다 — 본 ADR의 커밋 메커니즘이 그 이벤트 소스를 깐다(자리 열기).

## Considered Options

### 빌더 커밋 대상 (결정 1)
- **OKF 번들만 자동 커밋, 카드는 PR 유지(선택)** — "살아있는 지식"의 본체이자 잦은 갱신·admission 경계 아님이라 안전. MVP 한 바퀴 최소.
- **카드 YAML도 자동 커밋(기각)** — 카드는 등록 무결성 경계(유효 카드만 등록)라 자동 커밋이 admission을 우회할 위험. 잦게 바뀌지도 않는다(라우팅 메타). 검증→YAML→PR(T5.3) 유지가 안전.
- **둘 다 자동 커밋(기각)** — 카드 자동 커밋의 위험을 그대로 떠안으면서 범위만 키운다. 지금 필요한 건 OKF 한 바퀴.

### 저장소 구조 (결정 2)
- **모노repo 하위폴더 `okf/{agent_id}/`(선택)** — 현재 패턴 그대로·커밋/스냅샷/CODEOWNERS 다 줌·가벼움.
- **owner별 격리 repo(기각, 지금은)** — 진짜 데이터 격리(옵션 B)엔 맞지만 기본 경로엔 과함(repo N개 동기화·권한 운영). 후속 옵션.

### 커밋 메커니즘 (결정 3)
- **`GitGateway` 포트 + subprocess + Fake 주입(선택)** — `ClaudeRunner`·`AgentRuntime`과 같은 결정론 경계. 새 의존성 0.
- **GitPython 등 라이브러리(기각)** — 우리가 쓰는 연산(add·commit·rev-parse·archive)엔 subprocess가 충분. 의존 추가는 과함.
- **라이브 레지스트리 mutation으로 대체(기각)** — git/PR-as-SSOT와 충돌·비영속(CONTEXT Maintainer — 편집 채널 이중화 금지). 커밋이 진실 원천이어야 버전·감사가 산다.

### 커밋 스냅샷 실행 (결정 4)
- **`git archive` 추출 cwd + `Answer.snapshot_sha`(선택)** — 읽은 내용 = 그 SHA 트리(재현 보장). 워크트리 불필요·동시 읽기 충돌 0(ADR 0017 결정 3 실현).
- **working tree 직독 + SHA만 기록(기각)** — 기록 SHA와 실제 읽은 내용이 어긋날 수 있다(미커밋 변경·브랜치 전환). "이 답은 이 커밋 기준" 감사가 약해진다.

## Consequences

- **빌더 UI에 OKF 편집 면이 더해진다(후속 구현)** — 카드 면(검증→YAML 미리보기)과 *분리*된 OKF 번들 편집 면(마크다운 작성 → 빌더가 owner 신원으로 커밋). owner는 git을 몰라도 된다.
- **`Answer`에 `snapshot_sha: str | None` 추가** — 답이 어느 커밋 OKF로 만들어졌나(감사 메타). 기본 `None`(스냅샷 모드 아닐 때·기존 경로 하위호환). `mode`·`sources`와 같은 답 메타.
- **`GitGateway` 포트·`FakeGitGateway`·빌더 커밋 서비스 신설** — 결정론 단위 테스트. 실 git subprocess(`SubprocessGitGateway`)는 T8.1 (a)(b)로 구현 완료 — 이 환경에 git이 있어 tmp repo *통합 테스트*(행위 단언·SHA 값 비의존)로 게이트에 들였다. 경로 탈출 검증은 모듈 함수 `validate_okf_paths`(`OkfFile.path`)·`validate_agent_id`(번들 디렉터리명)로 빼 두 구현이 *같은 규칙*을 공유한다(안전 경계 단일화). `agent_id`는 `okf/{agent_id}/` 경로 쓰기와 `{sha}:{agent_id}` archive tree-ish에 박히므로 빈/공백·절대경로·`..`·경로구분자(`/`·`\`)를 거부해 okf_root 밖 쓰기·엉뚱한 트리 지목을 차단한다. `extract_snapshot`은 sha가 비었거나 `-`로 시작하면 거부(tree-ish 옵션 주입 방어)하고, 임시 tar는 시스템 temp에 두고 try/finally로 정리(답 cwd 오염·잔존 방지). T8.1 (a)(b) admission 정규식 강제는 후속 — 이번은 어댑터 안전 경계만.
- **`okf_root`의 의미 정밀화** — "owner 환경 루트" → "OKF 번들들을 담은 git repo 작업 트리 루트". working tree 직독(T6.7, 하위호환)과 커밋 스냅샷 실행(이 ADR)을 양립.
- **불변식 영향 없음** — 미아 없음·Authority 중앙(카드 자기보고 금지 — OKF는 답변 지식이지 권한 선언 아님)·전이≠기록(커밋은 git이 기록, 도메인 전이 아님)·등록 무결성(카드는 PR 유지)·노출 불변식(`snapshot_sha`는 운영 면 메타)은 그대로. 편집 채널은 git/PR 하나(빌더가 *그 채널로 커밋*할 뿐 — 라이브 mutation 이중화 아님, CONTEXT Maintainer·ADR 0013 안 B 정신).
- **갱신 대상**: CONTEXT(OKF·Knowledge Bundle·Answer·Agent Runtime·**GitGateway·OKF Builder 신규**), PRD §5(T7.2 진행), TRD §4(`Answer.snapshot_sha`·`GitGateway` 포트), tasks T7.2(설계·shape 완료 주석).
- **미해결로 남김**: 중앙 최신 읽기(pull/webhook 캐시 — 분산 repo·원격 동기화)·webhook 변경 전파(T7.3)·CODEOWNERS 실 강제(T7.1 SSO와 연결)·`snapshot_sha`의 채팅 노출 여부(sources 정신으로 후속)·owner별 repo 전환은 후속 결정.
