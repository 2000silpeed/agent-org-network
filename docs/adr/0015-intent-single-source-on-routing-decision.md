# intent를 RoutingDecision에 실어 단일 출처화한다 — 두 분류 호출 divergence 차단

상태: accepted (2026-06-21) · T6.2 선행 리팩터(LlmClassifier 도입 전) · **ADR 0028 정합(refine·무변경)**: 스케일 라우팅이 `intent` 1라벨 매칭을 `KnowledgeIndexMatcher` 개념 오버랩(다개념)으로 정밀화해도 `RoutingDecision`에 실리는 *단일 라우팅 키*는 그대로 하나다 — 매처가 고른 대표 개념(`matched_concept_id`)을 `intent` 자리에 실어 Precedent·ConflictCase·audit 색인을 보존(대표 키 선정 규칙은 ADR 0028 Open Questions·후속 정밀화).

## 맥락

`ask_org.handle`과 `router.route`가 *각자* `classifier.classify(question)`를 한 번씩 부른다. 한 질문 처리에 분류가 **두 번** 일어나고, 두 intent는 서로 모르는 채 따로 쓰인다.

- `router.route` — intent로 candidates를 매칭하고 판례를 lookup해 `RoutingDecision`을 만든다. intent는 *route 내부 지역변수*로만 살고 결정에 실리지 않는다.
- `ask_org.handle` — 자기 intent를 `ConflictCase`(어떤 분류 라벨의 다툼인가)와 `AuditEntry`(질문 처리 기록)에 쓴다.

지금 분류기는 `RuleBasedClassifier`·`FakeClassifier`라 **결정론적**이다 — 같은 질문 → 항상 같은 intent라 두 호출이 우연히 일치해 무해하다. 그러나 T6.2가 `LlmClassifier`(비결정 LLM 구현)를 들이면 이 우연한 일치가 깨진다.

- **상관관계 버그**: 같은 질문에 router의 intent와 ask_org의 intent가 *갈릴 수 있다*. router는 intent A로 Contested를 냈는데 ask_org가 intent B로 `ConflictCase`를 열면, 라우팅 처분과 케이스 라벨이 어긋난다(다툼이 엉뚱한 intent로 열려 합의·판례가 오염). audit의 intent도 라우팅이 실제로 본 intent와 달라진다.
- **낭비**: LLM 분류를 질문당 2회 호출한다(비용·지연 2배).

이건 "구현을 어떻게 짜나"가 아니라 "한 질문의 intent가 무엇인가"의 문제다 — **한 질문 처리에는 단 하나의 라우팅 intent가 있어야 한다.** 분류는 한 번 일어나고, 그 결과가 라우팅·케이스·기록에 *같은 값으로* 흘러야 한다.

## 결정

**`intent`를 `RoutingDecision`의 세 변이(`Routed`·`Contested`·`Unowned`) 모두에 `intent: str = ""` 필드로 싣는다.** 분류는 `router.route`가 한 번만 수행하고, 그 intent를 자기가 내는 결정에 실어 단일 출처(single source of truth)로 만든다. `ask_org`는 자기 classify를 *제거*하고 `decision.intent`를 읽는다.

세부:

1. **세 변이 모두에 필드 추가, 기본값 `""`.** sealed sum의 모든 변이가 "이 결정이 어떤 intent에서 나왔나"를 1급으로 안는다(`AuditEntry`가 decision·dispatch_outcome 원형을 안는 정신). 기본값 `""`이라 **기존 직접 생성처·`match` 사이트가 무영향**(Routed/Contested/Unowned를 인자 없이 만들던 모든 테스트가 안 깨짐) — `Routed.requires_approval`·`collaborators`를 기본값으로 더했을 때와 같은 하위호환 전략. router는 항상 채운다.

2. **래퍼(`ClassifiedDecision(intent, decision)`) 기각.** 대안은 `route`의 반환 타입을 바꿔 *모든 match 사이트*(ask_org·audit·web 직렬화·테스트)가 `.decision`을 한 단계 더 까야 한다 — 훨씬 침습적이고, sealed sum의 "타입이 곧 상태" 정신을 래퍼가 흐린다. intent는 결정의 *부속 사실*이지 결정을 감싸는 별 컨테이너가 아니다.

3. **`router.route`가 분류 단일 지점.** classify를 1회만 하고 그 intent를 판례 경로·0/1/≥2 분기가 내는 *모든* 결정에 싣는다. candidates를 매칭한 그 intent와 결정에 실리는 intent가 **구조적으로 같다**(같은 지역변수) — divergence가 원천 차단된다.

4. **`ask_org`는 classifier 의존을 뗀다.** `handle`이 classify를 호출하던 유일한 자리였으므로, `decision.intent`로 갈아끼우면 ask_org는 classifier가 더 이상 필요 없다 — 생성자 인자 `classifier`를 제거한다(단일 출처의 자연한 귀결: 분류 주체가 router 하나로 모이면 ask_org가 분류기를 들 이유가 없다). `ConflictCase.intent`·`open_for_intent`·`AuditEntry.intent` 모두 `decision.intent`를 넘긴다.

5. **`AuditEntry.intent`는 유지하되 출처는 `decision.intent` 하나.** audit의 intent는 질문 처리의 1급 기록이라 필드를 그대로 둔다(파생 프로퍼티로 바꾸지 않음 — `decision`이 None일 수 없고 항상 intent를 들지만, intent를 audit의 명시 필드로 남기는 게 기록 단위로 더 읽힌다). 단 ask_org가 채우는 값의 출처가 자기 classify가 아니라 `decision.intent`로 바뀐다.

## 노출 불변식 (불변)

intent는 *조직 내부값*이다 — 사용자向 `OrgReply`(Answered/Pending)에 **싣지 않는다**(이미 안 실리고, decision→OrgReply 투영이 intent를 떨군다). decision에 intent를 더해도 audit·내부 경로만 보고 사용자엔 새지 않는다. Contested가 "다툼이라는 사실조차 감춤"·intent 미노출도 그대로다.

## 결과

- **divergence 불가능.** 한 질문의 라우팅 intent가 하나로 고정돼, 라우팅 처분·ConflictCase 라벨·audit intent가 항상 같은 값이다. LLM 분류가 비결정이어도 *결정 안에 박힌 그 intent*를 모두가 본다.
- **LLM 호출 1회.** 질문당 분류 1회(비용·지연 절반).
- **결정론 검증 유지.** 이 리팩터는 `FakeClassifier`로 전부 결정론 검증한다. 광범위 테스트(Routed/Contested/Unowned 생성)는 intent 기본값 덕에 안 깨지고, router·ask_org·audit 경로는 intent 단일 출처를 새로 검증한다.
- **영향 범위.** `decision.py`(필드 +1×3)·`router.py`(세 분기 intent 임베드)·`ask_org.py`(classify 제거·classifier 인자 제거·decision.intent 소비)·`AskOrg` 생성처(demo.py + 테스트 fixture의 `classifier=` 전달 제거). `audit.py`는 무변경(ask_org가 넘기는 값의 출처만 바뀜).
- ADR 0008(ConflictCase)·0011(AuditEntry)·0003(결정론 검증)과 정합. CONTEXT(Intent·RoutingDecision·AuditEntry 절)·TRD §4·§6에 반영.
