"""run_eval 러너 *구조* 결정론 테스트 — 주입 분류기로 정확도·임계 통과를 검증한다.

ADR 0003 경계: 러너가 골든셋을 받아 ① 분류 정확도(classify 직접)·② 라우팅 정확도(route
결과)를 집계하고 임계와 비교하는 *로직*은 분류기와 무관하게 결정론이다. 여기서는 그 로직을
주입 분류기(golden oracle / broken / 임계 경계용)로만 검증한다 — 실 LlmClassifier·실 claude·
네트워크 0. 실 LLM 분류 품질은 CLI `--classifier llm`(게이트 밖)으로 본다.

ADR 0015 단일 출처: ②의 Router는 분류기를 *내장*한다(생성 시 주입). run_eval에 넘기는
분류기(①용)와 Router 내장 분류기를 **같은 인스턴스**로 둬야 일관 — 모든 케이스가 그렇게
한 인스턴스를 만들어 둘 다에 쓴다(CLI 구성과 동일).
"""

from datetime import date

from agent_org_network.agent_card import AgentCard
from agent_org_network.classifier import Classifier
from agent_org_network.eval import DEFAULT_ACCURACY_THRESHOLD, EvalReport, run_eval
from agent_org_network.golden import SampleQuestion
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.user import User

ROOT_USER = "root_manager"


# ── 인메모리 fixture: routed/contested/unowned 각 케이스를 덮는 최소 registry ──


def _card(agent_id: str, owner: str, domains: list[str]) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="t",
        summary="s",
        domains=domains,
        last_reviewed_at=date(2026, 6, 20),
    )


def _registry() -> Registry:
    reg = Registry()
    reg.register_user(User(id=ROOT_USER))
    reg.register_user(User(id="o1", manager=ROOT_USER))
    reg.register_user(User(id="o2", manager=ROOT_USER))
    # 계약 검토 → contract_ops 단일(routed). 보상 → cs_ops·finance_ops 둘(contested).
    reg.register(_card("contract_ops", "o1", ["계약 검토"]))
    reg.register(_card("cs_ops", "o1", ["환불", "보상"]))
    reg.register(_card("finance_ops", "o2", ["가격", "보상"]))
    reg.validate()
    return reg


# routed / contested / unowned 를 각각 덮는 4개 샘플(정확도 분모=4).
SAMPLES: list[SampleQuestion] = [
    SampleQuestion(
        question="이 계약서 검토해줘",
        expected_intent="계약 검토",
        expected_disposition="routed",
        expected_primary="contract_ops",
    ),
    SampleQuestion(
        question="환불 절차 알려줘",
        expected_intent="환불",
        expected_disposition="routed",
        expected_primary="cs_ops",
    ),
    SampleQuestion(
        question="보상 기준이 뭐야",
        expected_intent="보상",
        expected_disposition="contested",
        expected_candidates=["cs_ops", "finance_ops"],
    ),
    SampleQuestion(
        question="주차권 갱신은 어디서",
        expected_intent="주차",
        expected_disposition="unowned",
    ),
]


class _OracleClassifier:
    """각 질문을 그 expected_intent로 매핑한 결정론 분류기(golden oracle).

    question→intent dict로 완벽 분류를 흉내낸다. 같은 인스턴스를 Router·run_eval에 주입하면
    분류 정확도 1.0, 라우팅도 골든 disposition과 일치(ADR 0015 단일 출처 일관).
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def classify(self, question: str) -> str:
        return self._mapping.get(question, "")


class _BrokenClassifier:
    """항상 ""를 내는 분류기 — 분류·라우팅 둘 다 빗나간다(미아 없음: 전부 Unowned)."""

    def classify(self, question: str) -> str:
        return ""


def _oracle() -> _OracleClassifier:
    return _OracleClassifier({s.question: s.expected_intent for s in SAMPLES})


def _run(classifier: Classifier, threshold: float = DEFAULT_ACCURACY_THRESHOLD) -> EvalReport:
    """ADR 0015: 같은 분류기 인스턴스를 Router·run_eval에 주입해 일관 유지."""
    router = Router(_registry(), classifier, root_user=ROOT_USER)
    return run_eval(SAMPLES, classifier, router, threshold=threshold)


# ── (a) golden oracle → 두 정확도 1.0·passed=True ────────────────────────────


def test_oracle_분류기는_분류_정확도_1점0():
    report = _run(_oracle())
    assert report.classification_accuracy == 1.0


def test_oracle_분류기는_라우팅_정확도_1점0():
    # 각 질문이 expected_intent로 분류 → Router가 골든 disposition(routed/contested/unowned)과
    # 정확히 일치(primary·candidates까지).
    report = _run(_oracle())
    assert report.routing_accuracy == 1.0


def test_oracle_분류기는_passed_True():
    assert _run(_oracle()).passed is True


def test_oracle_report_필드_검증():
    report = _run(_oracle())
    assert isinstance(report, EvalReport)
    assert report.total == len(SAMPLES)
    assert report.threshold == DEFAULT_ACCURACY_THRESHOLD
    assert report.classification_accuracy == 1.0
    assert report.routing_accuracy == 1.0
    assert report.passed is True


# ── (b) broken 분류기 → 두 정확도 낮음·passed=False ──────────────────────────


def test_broken_분류기는_분류_정확도가_낮다():
    report = _run(_BrokenClassifier())
    # 모든 질문 ""로 분류 → expected_intent와 일치하는 건 없다(주차도 ""가 아님)
    assert report.classification_accuracy == 0.0


def test_broken_분류기는_passed_False():
    assert _run(_BrokenClassifier()).passed is False


def test_broken_분류기_라우팅은_unowned만_맞는다():
    # "" → Router 전부 Unowned. 골든 4개 중 unowned 1개만 라우팅 일치 → 0.25.
    report = _run(_BrokenClassifier())
    assert report.routing_accuracy == 0.25


# ── (c) 임계 경계 — 같은 결과를 threshold만 바꿔 통과/실패를 가른다 ──────────


class _PartialClassifier:
    """절반만 맞히는 분류기 — 임계 경계 검증용.

    4개 중 2개(계약 검토·환불, 둘 다 routed)만 expected_intent로, 나머지는 ""로 분류한다.
    분류 정확도 0.5. 라우팅: 맞힌 2개는 routed 일치, 틀린 2개("")는 Unowned가 되는데 그중
    골든 unowned(주차) 1개가 우연히 일치 → 라우팅 정확도 0.75.
    """

    def __init__(self) -> None:
        self._mapping = {
            "이 계약서 검토해줘": "계약 검토",
            "환불 절차 알려줘": "환불",
        }

    def classify(self, question: str) -> str:
        return self._mapping.get(question, "")


def test_임계_경계_분류_정확도_0점5_threshold_0점5면_분류는_통과선():
    # threshold=0.5: 분류 정확도 0.5 >= 0.5 (통과 조건 충족). 단 라우팅 0.75도 >= 0.5라
    # 전체 passed=True.
    report = _run(_PartialClassifier(), threshold=0.5)
    assert report.classification_accuracy == 0.5
    assert report.routing_accuracy == 0.75
    assert report.passed is True


def test_임계_경계_threshold_0점8이면_분류_0점5라_실패():
    # threshold=0.8: 분류 정확도 0.5 < 0.8 → passed=False(둘 중 하나라도 미달이면 실패).
    report = _run(_PartialClassifier(), threshold=0.8)
    assert report.passed is False


def test_임계_경계_둘_다_임계_이상이어야_통과():
    # 라우팅은 0.75로 0.7 이상이지만 분류 0.5가 0.7 미만 → 실패(AND 조건).
    report = _run(_PartialClassifier(), threshold=0.7)
    assert report.routing_accuracy >= 0.7
    assert report.classification_accuracy < 0.7
    assert report.passed is False


# ── 빈 골든셋 방어 ───────────────────────────────────────────────────────────


def test_빈_골든셋이면_passed_False_total_0():
    router = Router(_registry(), _oracle(), root_user=ROOT_USER)
    report = run_eval([], _oracle(), router)
    assert report.total == 0
    assert report.classification_accuracy == 0.0
    assert report.routing_accuracy == 0.0
    assert report.passed is False
