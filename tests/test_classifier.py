from agent_org_network.classifier import FakeClassifier, RuleBasedClassifier


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
