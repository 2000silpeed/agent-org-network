# 운영 면 신원은 무비밀번호 세션에서 오고, 신원 출처를 path/body에서 세션으로 옮긴다

상태: accepted (2026-06-21)

ADR 0009는 페르소나별 면이 *인증으로 분리된 별개 공간*이고 (1) 실 사용자 면과 운영 면 분리, (2) Owner는 자기 소유 카드의 처리함만 접근, (3) `inbox.html`의 owner 가장 드롭다운 제거를 최종 완료 기준으로 못박았다. 그 "인증만 끼우면"의 *인증*을 v0에서 어떻게 실체화할지, 그리고 그게 어디까지인지를 이 ADR이 확정한다. PRD §6은 SSO·정식 인증을 범위 밖으로 두고 v0는 *최소 인증*으로 한정한다.

## 결정

**1. 세션 메커니즘 = 서명 쿠키 세션(무비밀번호).** 운영 면 진입에 *세션 신원* 1개를 요구한다. 자격증명·비밀번호·SSO·OAuth·JWT·헤더 토큰은 v0 범위 밖(PRD §6 후속) — v0는 *신원 선택*(무비밀번호)을 세션에 고정해 *per-request 가장*을 차단하는 것까지다. secret key는 **env/주입**이며 커밋하지 않는다(테스트는 고정 키 주입, 운영은 env). `create_app`·`create_central_app`에 `session_secret` 주입 인자를 더한다.

**1-a. itsdangerous 미설치 — 서명 메커니즘은 starlette `SessionMiddleware` 채택을 전제로 의존성 1개를 추가한다.** starlette `SessionMiddleware`는 `itsdangerous.TimestampSigner`로 서명 쿠키를 구현하는데 이 저장소엔 `itsdangerous`가 없다(확인됨 — import 실패). 두 길의 트레이드오프:
  - (A) `itsdangerous`를 의존성에 추가하고 `SessionMiddleware`를 쓴다 — 검증된 서명·만료, 코드 최소(`request.session` dict 접근). 의존성 1개 증가.
  - (B) 서명을 직접 구현(hmac+base64) — 의존성 0, 그러나 *보안 민감 코드를 자작*(타이밍 공격·만료·롤링 키)이라 v0 최소 인증에 과하고 위험하다.
  **(A) 채택** — 보안 코드 자작 회피가 무비밀번호 v0에서도 옳다(서명 검증은 직접 짤 영역이 아님). `itsdangerous`는 starlette의 표준 세션 의존성이라 생태계 정합. 다만 *세션 신원을 읽는 우리 코드*는 미들웨어에 직접 묶지 않고 **헬퍼 한 곳**(`_session_identity(request)`)으로 격리해, 후속에 메커니즘이 바뀌어도(JWT 등) 엔드포인트가 안 흔들리게 한다(헥사고날 정신 — 엔드포인트는 "세션 신원 1개"라는 추상만 본다).

**2. 로그인 흐름 = `POST /login`(body `user_id`) + `POST /logout`.** `user_id`는 *Registry에 존재하는 User*여야 한다(`registry.user_ids()` 검사 — 없으면 401). 성공 시 세션에 `user_id`를 저장한다. `POST /logout`은 세션을 클리어한다. 무비밀번호라 *신원 선택*이지만 세션이 1신원을 고정하므로 per-request 가장은 불가(ADR 0009 "페르소나 혼입 금지"의 실현). User 존재 검사의 출처는 `bundle`이 보는 Registry다 — 데모 6명(root_manager·legal_lead·cs_lead·finance_lead·hr_lead·it_lead)이 유효 신원.

**3. 신원 출처 이동(핵심 보안) = path/body → 세션.** 운영 엔드포인트는 신원을 *세션에서* 읽는다. body의 `by_owner`·`by_manager`와 path의 `{owner_id}`·`{manager_id}`는 **더 이상 신원의 출처가 아니다**. 두 규칙:
  - **path param 제거(owner/manager 자기 면)**: `GET /inbox/{owner_id}` → `GET /inbox/cases`(세션 owner). `GET /inbox/{owner_id}/backup-reviews` → `GET /inbox/backup-reviews`(세션 owner). `GET /manager/{manager_id}` → `GET /manager/queue`(세션 신원). path에 신원을 안 실으면 "남의 것을 path로 지목"할 표면 자체가 없어진다 — ADR 0009 "자기 처리함만"이 *구조적으로* 강제된다(403 검사보다 깔끔).
  - **body 1인칭은 세션에서 채움**: `POST /cases/{case_id}/concur`·`POST /backup-reviews/{item_id}`·`POST /manager/items/{item_id}/act`는 요청 body에서 `by_owner`/`by_manager`를 *받지 않고* 세션 신원으로 채운다. 도메인 서비스(ConsensusService·BackupReviewService·ManagerQueueService)의 1인칭 강제(`item.owner_id == by_owner` 등 ValueError)는 그대로 — 단 그 1인칭 값의 *출처가 세션*이라 위조가 불가능해진다. `case_id`·`item_id`는 신원이 아니라 *대상 지목*이라 path/body에 남는다(스코프 검사로 보호).

**4. 스코프 강제 = 자기 것만.** 세션 신원이 그 대상의 owner/manager가 아니면 거부한다.
  - concur: 세션 owner가 그 case의 후보 owner가 아니면 — 도메인이 이미 ValueError(후보 owner 아님)로 막으므로 **403**으로 매핑(현재 400을 스코프 위반은 403으로). case가 자기 처리함에 없으면 못 본다.
  - backup-review: 세션 owner ≠ item.owner_id면 도메인 ValueError → **403**.
  - manager act: 세션 신원 ≠ item.manager_id면 도메인 ValueError → **403**.
  스코프 위반(자기 권한 밖)은 **403**, 대상 미존재는 **404**, 미로그인은 **401**로 가른다. 입력 형식 오류(예: 알 수 없는 case_id 형식)는 기존 400 유지.

**5. 역할(role)은 그래프에서 파생, 깊은 RBAC 없음.** Owner-면이냐 Manager-면이냐는 별도 role DB가 아니라 *그래프*가 정한다 — 세션 User가 어떤 카드를 owns면 그 처리함을 보고(Owner면), 자기 manager_id의 큐만 본다(Manager면). v0가 강제하는 핵심은 **기준 2(Owner 자기 처리함 스코프)**다. Manager 큐·모니터링은 "**인증된 사용자**" 게이트(로그인 필요) + 자기 귀속(manager는 자기 큐)까지. 모니터링(`/monitor*`)은 *세분 역할 DB 없이* **인증만** 요구한다(로그인하면 봄 — 운영자 역할 분리는 후속). 권한 매트릭스·역할 테이블·세분 RBAC는 범위 밖.

**6. 실 사용자 채팅은 익명 유지(다른 공간).** `POST /ask`·`GET /ask/{tracking}`·`GET /`(index.html)는 운영 세션을 요구하지 않는다 — ADR 0009의 "별개 공간". "페르소나 혼입 금지"는 *한 세션이 운영 신원 1개로 고정*되고 채팅은 인증 경계 밖 별 공간이라는 두 축으로 실현된다. 채팅의 노출 불변식(OrgReply 내부값 미노출)은 그대로다.

**7. 가장 드롭다운 제거 = 로그인 폼으로 대체.** `inbox.html`의 owner `<select>`(하드코딩 4개)와 `currentOwner()`를 제거하고, 세션 신원을 *읽기 전용*으로 표시한다. 미로그인이면 로그인 폼(`user_id` 입력 → `POST /login`)을 보이고, 로그인되면 신원 + 로그아웃을 보인다. fetch 경로에서 owner를 path로 싣던 것(`/inbox/<owner>`)을 세션 경로(`/inbox/cases`)로 바꾼다.

## Consequences

- **기존 테스트가 세션 라운드로 바뀐다.** `/inbox/{owner}`·concur·manager·backup-review를 직접 호출하던 테스트(`test_web.py`·`test_t5_2_web_regression.py`·`test_6_6_iii_fixes.py`·backup-review 통합 테스트들)는 이제 **로그인 후** 세션 신원으로 호출해야 한다. `TestClient`는 쿠키를 유지하므로 `client.post("/login", json={"user_id": "cs_lead"})` 한 번이면 이후 요청에 세션이 실린다. body의 `by_owner`/`by_manager`는 빠지고, path의 `{owner_id}`/`{manager_id}`는 사라진다. 이건 *의도된 와이어 변경*이라 회귀가 아니라 *재배선*이다(게이트 495 → 세션 테스트 추가/재배선 후 그린 회복).
- **신규 스코프 테스트**: 미로그인 401 · 로그인 후 자기 것 200 · 남의 inbox/큐 403(세션 owner ≠ 대상) · 미존재 대상 404 · 로그인/로그아웃 라운드.
- **결정론**: 고정 `session_secret` 주입 + `TestClient` 쿠키 유지로 로그인→접근이 결정론. 실 브라우저 세션·실 인증서버 0(무비밀번호라 외부 의존 0).
- **레거시 path 라우트는 인증 OFF 전용 등록 + 모듈 기본 앱은 env secret(보안 — 코드리뷰 B-1·B-2)**: `session_secret` 미주입이면 하위호환으로 `/inbox/{owner_id}`·`/manager/{manager_id}` 등 *path 가장 경로*를 등록하지만, **`session_secret` 주입(인증 ON)이면 그 path 경로를 *등록하지 않는다*(404)** — 안 그러면 로그인 없이 `GET /inbox/cs_lead`로 남의 처리함을 읽어 세션 스코프가 통째로 우회된다. 두 웹 팩토리(`create_app`·`create_central_app`) *모두* `session_secret`을 받고(둘 중 하나라도 빠지면 그 진입점이 무방비), 모듈 기본 앱(`web.app`·`server.central_app`)은 **`OPERATOR_SESSION_SECRET` env**에서 secret을 읽는다 — **env 설정 = 프로덕션 인증 ON, env 미설정 = 데모 인증 OFF**(fail-open은 *의도된 데모 모드*임을 여기 명시; 프로덕션 배포는 env 필수). 회귀 테스트 `test_security_regression.py`가 인증 ON에서 레거시 path 404·미로그인 401·env 분기를 박는다.
- **연결점(범위 밖)**: SSO·OAuth·비밀번호 해시·RBAC 매트릭스·CSRF 토큰·rate limit·세션 만료 정책 튜닝은 후속. 워커 인증(ADR 0011·0012의 `_authenticate` hook·등급 검증)은 *전송* 인증이라 이 ADR(운영 *웹 면* 인증)과 별 축이지만, 같은 "진짜 그 owner인가"를 다른 채널에서 강제한다(연결점만).
- **불변식 무관**: 인증은 *접근 경계*이지 라우팅·답 도메인이 아니라 미아 없음·Authority 중앙·전이≠기록을 건드리지 않는다. 운영 면 내부값 노출은 *인증된 운영자가 본다*는 전제에서 여전히 OK(처리함·Manager 큐·모니터링), 채팅 노출 불변식은 유지.
- ADR 0009를 폐기하지 않고 *구체화*한다(0009가 governs, 0016이 메커니즘·스코프·실패 코드를 박음).
