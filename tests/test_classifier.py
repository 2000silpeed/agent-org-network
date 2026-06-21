from agent_org_network.classifier import (
    FakeClassifier,
    LlmClassifier,
    RuleBasedClassifier,
)


def test_fake_classifier는_고정_intent를_반환한다():
    classifier = FakeClassifier("계약 검토")
    assert classifier.classify("아무 질문이나") == "계약 검토"


def test_rulebased_classifier는_키워드로_intent를_고른다():
    classifier = RuleBasedClassifier({"계약": "계약 검토", "환불": "CS 정책"})
    assert classifier.classify("이 고객 계약 조건 바꿔도 돼?") == "계약 검토"
    assert classifier.classify("환불 되나요?") == "CS 정책"


def test_rulebased_classifier는_매칭_없으면_default를_반환한다():
    classifier = RuleBasedClassifier({"계약": "계약 검토"}, default="")
    assert classifier.classify("주차장 정기권 어떻게 갱신해요?") == ""


# ── LlmClassifier (fake runner 주입 — 실 claude -p 0) ────────────────────────
#
# 결정론 경계(ADR 0003·0010): 실 LLM은 비결정·느리므로 FakeRunner(고정 응답)를 주입한다.
# 여기서는 (1) build_prompt에 intent 후보가 실리는지, (2) 정상 라벨 파싱, (3) 어휘 외·형식
# 오류·여러 줄·미지를 ""로 흡수(미아 없음 보존), (4) 빈 intents → 항상 ""를 검증한다.

INTENTS = ["계약 검토", "환불", "보상", "접근권한"]


class _FakeRunner:
    """프롬프트를 받아 고정 응답을 돌려주며 마지막 프롬프트를 기록한다(실 claude 대체)."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_prompt: str | None = None

    def __call__(self, prompt: str, /) -> str:
        self.last_prompt = prompt
        return self.reply


def test_llm_classifier_build_prompt에_intent_후보가_모두_실린다():
    runner = _FakeRunner("환불")
    clf = LlmClassifier(INTENTS, runner=runner)

    prompt = clf.build_prompt("환불 되나요?")

    for intent in INTENTS:
        assert intent in prompt, f"후보 '{intent}'가 프롬프트에 없음"
    # 질문 본문과 '하나만/없으면 빈 줄' 지시가 실린다
    assert "환불 되나요?" in prompt
    assert "빈 줄" in prompt


def test_llm_classifier_정상_라벨_응답을_그대로_파싱한다():
    clf = LlmClassifier(INTENTS, runner=_FakeRunner("환불"))
    assert clf.classify("지난달 결제 환불해줘") == "환불"


def test_llm_classifier_공백_따옴표_감싼_응답도_라벨로_정규화한다():
    assert LlmClassifier(INTENTS, runner=_FakeRunner('  "환불"  ')).classify("q") == "환불"
    assert LlmClassifier(INTENTS, runner=_FakeRunner("접근권한.\n")).classify("q") == "접근권한"
    assert LlmClassifier(INTENTS, runner=_FakeRunner("`계약 검토`")).classify("q") == "계약 검토"


def test_llm_classifier_어휘_밖_응답은_빈_문자열로_흡수한다():
    # 환각·집합 밖 라벨 → "" (Router 0매칭 → Unowned, 미아 없음)
    clf = LlmClassifier(INTENTS, runner=_FakeRunner("주차"))
    assert clf.classify("주차 정기권 갱신") == ""


def test_llm_classifier_미지_응답은_빈_문자열이다():
    # LLM이 '모름' 류 자유서술을 내도 어휘 밖이라 "" — 부분일치 승격 안 함
    clf = LlmClassifier(INTENTS, runner=_FakeRunner("잘 모르겠습니다"))
    assert clf.classify("???") == ""


def test_llm_classifier_빈_응답은_빈_문자열이다():
    assert LlmClassifier(INTENTS, runner=_FakeRunner("   \n  ")).classify("q") == ""


def test_llm_classifier_여러_줄_잡음_응답에서_유효_라벨_줄을_고른다():
    # 한 줄 지시를 어기고 여러 줄이 와도, 정확히 일치하는 라벨 줄이 있으면 그걸 고른다
    clf = LlmClassifier(INTENTS, runner=_FakeRunner("분류 결과는 다음과 같습니다:\n환불\n끝"))
    assert clf.classify("환불 문의") == "환불"


def test_llm_classifier_여러_줄에_유효_라벨_없으면_빈_문자열이다():
    clf = LlmClassifier(INTENTS, runner=_FakeRunner("첫 줄\n둘째 줄\n셋째 줄"))
    assert clf.classify("q") == ""


def test_llm_classifier_부분일치는_라벨로_승격하지_않는다():
    # "환불 정책"은 "환불"을 포함하지만 정확히 일치하지 않으므로 "" (근사매칭 금지)
    clf = LlmClassifier(INTENTS, runner=_FakeRunner("환불 정책"))
    assert clf.classify("q") == ""


def test_llm_classifier_빈_intents면_runner_없이도_항상_빈_문자열():
    # 고를 어휘가 없으면 LLM을 부르지 않는다(runner 미주입이어도 실 claude 호출 0)
    clf = LlmClassifier([])
    assert clf.classify("아무 질문") == ""


def test_llm_classifier_빈_intents면_runner를_호출하지_않는다():
    runner = _FakeRunner("환불")
    clf = LlmClassifier([], runner=runner)
    assert clf.classify("환불 되나요?") == ""
    assert runner.last_prompt is None, "빈 intents인데 runner가 호출됨"
