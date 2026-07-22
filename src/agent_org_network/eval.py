"""골든셋 eval 러너 — 분류·라우팅 정확도를 임계값으로 측정한다(T6.2).

ADR 0003·TRD §7: "eval(통계) — 골든셋(Precedent + 샘플 질문) 정확도/통과율 임계값,
분류기·런타임 변경 시·야간 회귀 게이트". **한 예제가 틀리는 게 실패가 아니라 임계 미달·
이전 대비 하락이 실패**다(CONTEXT Golden set).

T6.4 골든셋 데이터(`samples/questions.jsonl` + `golden.py`)를 소비한다 — 데이터(분류기 무관)와
러너(데이터 소비)의 분리. 단위 테스트(결정론)가 *로직 오류*를 매 커밋 잡고, 이 러너는
*확률적 LLM 분류 품질*을 임계 eval로 잡는다.

두 정확도(메트릭):
  ① **분류 정확도** — `classifier.classify(question) == expected_intent` 비율. 분류기를
     *직접* 부른다(분류 품질 측정이 목적). 라우팅 intent 단일 출처화(ADR 0015) 후에도
     eval은 분류 품질을 따로 봐야 하므로 route가 아니라 classify를 잰다.
  ② **라우팅 정확도** — `router.route(question)`의 disposition(+ routed면 primary,
     contested면 candidates)이 expected와 일치하는 비율. route 결과로 잰다.
두 메트릭을 깔끔히 분리: ①은 classify 직접, ②는 route 결과 — 한 질문에 둘을 각각 잰다
(같은 골든셋 30개에 대해).

**갭 라벨 채점 보정(어휘 사영, vocabulary projection)**: 골든셋(`samples/questions.jsonl`)
unowned 갭 질문 일부(주차·구내식당·복지·시설)의 `expected_intent`는 registry 어휘(카드
domains 합집합) 밖 라벨이다(사람 가독 라벨을 그대로 유지 — provenance 보존, ADR 0041).
`LlmClassifier`의 계약(classifier.py)은 어휘 밖 환각을 `""`로 정규화하므로, 이 질문들은
분류기가 계약대로 `""`를 내도(정답) `classify(q) != expected_intent`가 되어 자동 미스가
된다. `run_eval(..., vocabulary=...)`로 어휘를 주입하면 분류 축 비교를 어휘 사영으로
보정한다: `proj(expected, vocab) = expected if expected in vocab else ""`, hit ⇔
`classify(q) == proj(expected_intent, vocab)`. 골든 라벨 자체는 바꾸지 않고 *채점*만
보정 — `vocabulary=None`(기본)이면 기존 정확 일치 그대로(하위호환). 라우팅·답변 품질
축은 이 보정과 무관.

③ **답변 품질 정확도**(ADR 0041 — 0003 완성, 신규 확장 아님): `SampleQuestion.answer_expectation`
(사람이 확정한 provenance 필수 — `golden.py`)이 있고 `route(question)`이 `Routed`로 떨어진
질문만 `runtime.answer(question, decision.primary)`로 답변 경로를 재생해 `AnswerGrader`로
채점한다. Contested/Unowned는 단일 primary가 없어 스코프 밖(채점 안 함). `runtime`·`grader`
둘 다 주입돼야 도는 *옵트인* 축이라, 미주입이면 기존 두 축만(하위호환·회귀 0). 채점 대상이
0건이면 정확도는 0.0이 아니라 `None`(분모 없는 비율의 오분류를 막는다).

결정론 vs 실 LLM 경계(ADR 0003·ADR 0041 결정 4):
  - **러너 *구조*는 결정론 테스트**다 — 주입한 분류기가 expected/wrong intent를 내면
    정확도 계산·임계 통과/실패가 맞나를 `FakeClassifier`로 검증(tdd-engineer). 러너가
    골든셋을 받아 메트릭을 집계하고 임계와 비교하는 *로직*은 분류기와 무관하게 결정론.
    답변 품질 축도 동형 — `StubRuntime`+`FakeGrader`(고정 판정)로 러너 집계 로직만
    결정론 검증한다.
  - **실 `LlmClassifier`·실 `LlmGrader`로 도는 정확도 측정은 게이트 밖**(eval/수동·야간 —
    실 LLM, TRD §7). CI 매 커밋 게이트가 아니라 분류기·grader 변경 시·야간 회귀로 돈다.

CLI 진입점 `main`(`python -m agent_org_network.eval`) — 골든셋 경로·분류기 선택(rule/llm)·
임계값을 인자로 받아 EvalReport를 출력하고 통과/실패를 종료 코드로 돌려준다.

범위 밖(자리만): 임베딩 유사도·다중 LLM·프롬프트 튜닝·캐싱·confusion matrix 시각화.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from agent_org_network.classifier import Classifier
from agent_org_network.decision import Contested, RoutingDecision, Routed, Unowned
from agent_org_network.golden import AnswerExpectation, SampleQuestion
from agent_org_network.router import Router
from agent_org_network.runtime import AgentRuntime, Answer

# 기본 통과 임계(예시) — 분류·라우팅 정확도가 이 비율 미만이면 실패. 실 값·이전 대비
# 하락 판정은 구현/운영에서 확정(CONTEXT Golden set "임계 미달·이전 대비 하락이 실패").
# ADR 0041 결정 1: 답변 품질 축도 *같은* 임계를 공유한다(별도 임계 신설 없음).
DEFAULT_ACCURACY_THRESHOLD = 0.8


@dataclass(frozen=True)
class Grade:
    """답변 채점 1건의 결과 — 통과 여부 + 근거(디버깅용 후속 필드)."""

    passed: bool
    reason: str = ""


class AnswerGrader(Protocol):
    """답변 품질 채점 포트(ADR 0041 결정 1) — `run_eval`이 답변 축 재생 결과를 이걸로 재판정.

    `FakeGrader`(결정론 구조테스트)·`SubstringGrader`(결정론·게이트 내)·`LlmGrader`
    (실 LLM-as-judge·게이트 밖) 세 구현이 이 포트를 만족한다.
    """

    def grade(self, answer: Answer, expectation: AnswerExpectation, question: str) -> Grade: ...


class FakeGrader:
    """고정 판정을 내는 결정론 `AnswerGrader` — 러너 집계 로직만 검증(tdd-engineer)."""

    def __init__(self, verdict: bool) -> None:
        self._verdict = verdict

    def grade(self, answer: Answer, expectation: AnswerExpectation, question: str) -> Grade:
        return Grade(passed=self._verdict, reason="FakeGrader 고정 판정")


class SubstringGrader:
    """결정론·게이트 내 grader — `expectation.criteria`가 `answer.text`에 포함되나로 채점.

    `match="all"`이면 criteria 전부, `match="any"`면 하나 이상 포함돼야 통과. 빈 criteria는
    "all"이면 공허하게 통과(포함해야 할 게 없음), "any"면 통과할 게 없어 실패로 취급한다.
    """

    def grade(self, answer: Answer, expectation: AnswerExpectation, question: str) -> Grade:
        hits = [c for c in expectation.criteria if c in answer.text]
        if expectation.match == "all":
            passed = len(hits) == len(expectation.criteria)
        else:
            passed = len(hits) > 0
        reason = f"{len(hits)}/{len(expectation.criteria)} criteria matched (match={expectation.match})"
        return Grade(passed=passed, reason=reason)


class LlmGrader:
    """실 LLM-as-judge grader — **게이트 밖·미구현 자리**(ADR 0041 결정 4)."""

    def grade(self, answer: Answer, expectation: AnswerExpectation, question: str) -> Grade:
        raise NotImplementedError("실 LLM-as-judge grader는 게이트 밖(수동/야간 eval)")


@dataclass(frozen=True)
class EvalReport:
    """골든셋 eval 1회의 결과 — 세 정확도(①②③)와 임계 통과 여부.

    frozen 값 객체로 메트릭을 안는다(러너가 산출). `classification_accuracy`(①)·
    `routing_accuracy`(②)·`answer_quality_accuracy`(③, ADR 0041)·`total`(평가한 질문 수)·
    `answer_graded`(③의 분모 — 채점된 질문 수)·`threshold`(적용 임계)·`passed`(①②는 항상,
    ③은 채점 대상이 있을 때만 임계 이상인가). 한 예제 틀림이 아니라 *집계 비율 vs 임계*가
    통과를 가른다. `answer_quality_accuracy`는 `answer_graded==0`이면 `None`(0.0 아님 —
    분모 없는 비율을 0으로 위장하지 않는다). 개별 미스는 디버깅용 후속 필드 — MVP는
    집계 비율·통과 여부만.
    """

    classification_accuracy: float
    routing_accuracy: float
    total: int
    threshold: float
    passed: bool
    answer_quality_accuracy: float | None = None
    answer_graded: int = 0


def run_eval(
    samples: list[SampleQuestion],
    classifier: Classifier,
    router: Router,
    threshold: float = DEFAULT_ACCURACY_THRESHOLD,
    runtime: AgentRuntime | None = None,
    grader: AnswerGrader | None = None,
    vocabulary: Sequence[str] | None = None,
) -> EvalReport:
    """골든셋에 대해 분류·라우팅(+옵트인 답변 품질) 정확도를 재고 임계와 비교한다.

    각 SampleQuestion에 대해 ① classify(question)==expected_intent(분류 정확도)와
    ② route(question) disposition/primary/candidates==expected(라우팅 정확도)를 집계해
    두 비율을 낸다. classifier를 *직접* 부르는 게 ①의 핵심(분류 품질 측정 — route의 단일
    intent와 별도). router는 ②를 위해 받는다.

    `vocabulary`(옵트인, 갭 라벨 채점 보정): 주어지면 ①의 비교를 어휘 사영으로 보정한다 —
    `expected_intent`가 `vocabulary` 밖이면 `""`로 사영해 비교(`classify(q) == proj`).
    골든셋 unowned 갭 질문(주차 등)의 `expected_intent`가 registry 어휘 밖이라, 분류기가
    계약대로 `""`를 내도(정답) 정확 일치로는 자동 미스가 되는 걸 보정한다. `None`(기본)이면
    기존 정확 일치 그대로(하위호환·회귀 0). 라우팅·답변 품질 축은 이 보정과 무관.

    ③ 답변 품질(ADR 0041, 옵트인): `runtime`·`grader`가 둘 다 주입되고, 그 질문의
    `sample.answer_expectation`이 있고, `route(question)`이 `Routed`로 떨어졌을 때만
    `runtime.answer(question, decision.primary)`를 재생해 `grader.grade(...)`로 채점한다.
    Contested/Unowned는 단일 primary가 없어 스코프 밖(채점하지 않음). 하나라도 미주입/미해당
    이면 그 질문은 답변 축 집계에서 빠진다 — `runtime`·`grader` 모두 None(기본값)이면 답변
    축 전체가 skip돼 기존 두 축 리포트와 100% 동일(하위호환·회귀 0).

    통과 조건: 분류·라우팅 정확도는 항상 threshold 이상이어야 하고, 답변 품질은 채점
    대상이 있을 때만(`answer_quality_accuracy is not None`) threshold 이상이어야 한다
    (채점 대상 0건이면 그 축은 판정에서 빠진다 — None은 미달이 아니다).

    결정론 테스트: `FakeClassifier`(expected/wrong intent)·`FakeGrader`(고정 판정)를
    주입하면 정확도·통과/실패가 예측대로 나오는지 검증한다(러너 구조 결정론). 실
    `LlmClassifier`·`LlmGrader` 주입은 eval/수동·야간.
    """
    total = len(samples)
    if total == 0:
        return EvalReport(
            classification_accuracy=0.0,
            routing_accuracy=0.0,
            total=0,
            threshold=threshold,
            passed=False,
            answer_quality_accuracy=None,
            answer_graded=0,
        )

    classification_hits = 0
    routing_hits = 0
    answer_hits = 0
    answer_graded = 0
    for sample in samples:
        expected_intent = _project_to_vocabulary(sample.expected_intent, vocabulary)
        if classifier.classify(sample.question) == expected_intent:
            classification_hits += 1
        decision = router.route(sample.question)
        if _routing_matches(decision, sample):
            routing_hits += 1
        if (
            sample.answer_expectation is not None
            and runtime is not None
            and grader is not None
            and isinstance(decision, Routed)
        ):
            answer = runtime.answer(sample.question, decision.primary)
            grade = grader.grade(answer, sample.answer_expectation, sample.question)
            answer_graded += 1
            if grade.passed:
                answer_hits += 1

    classification_accuracy = classification_hits / total
    routing_accuracy = routing_hits / total
    answer_quality_accuracy = (answer_hits / answer_graded) if answer_graded > 0 else None
    passed = (
        classification_accuracy >= threshold
        and routing_accuracy >= threshold
        and (answer_quality_accuracy is None or answer_quality_accuracy >= threshold)
    )
    return EvalReport(
        classification_accuracy=classification_accuracy,
        routing_accuracy=routing_accuracy,
        total=total,
        threshold=threshold,
        passed=passed,
        answer_quality_accuracy=answer_quality_accuracy,
        answer_graded=answer_graded,
    )


def _project_to_vocabulary(expected_intent: str, vocabulary: Sequence[str] | None) -> str:
    """갭 라벨 채점 보정 — `vocabulary` 밖 라벨을 `""`로 사영한다(분류 축 채점 전용).

    `vocabulary`가 `None`이면 사영하지 않고 그대로 돌려준다(하위호환: 기존 정확 일치).
    """
    if vocabulary is None or expected_intent in vocabulary:
        return expected_intent
    return ""


def _routing_matches(decision: RoutingDecision, sample: SampleQuestion) -> bool:
    """route 결정이 골든 라벨의 disposition(+ primary/candidates)과 일치하나(라우팅 정확도).

    disposition 매칭:
      - routed:    Routed이고 `primary.agent_id == expected_primary`.
      - contested: Contested이고 candidate agent_id 집합 == set(expected_candidates).
      - unowned:   Unowned면 disposition만(primary/candidates 없음).
    Approval·Collaborator는 라우팅 정확도 스코프 밖(분류·dispatch 정확도 측정이 목적) —
    골든셋의 disposition/primary/candidates만 본다(이 러너의 계약).
    """
    disposition = sample.expected_disposition
    if disposition == "routed":
        return (
            isinstance(decision, Routed)
            and decision.primary.agent_id == sample.expected_primary
        )
    if disposition == "contested":
        if not isinstance(decision, Contested):
            return False
        actual = {c.agent_id for c in decision.candidates}
        return actual == set(sample.expected_candidates or [])
    if disposition == "unowned":
        return isinstance(decision, Unowned)
    return False


# ── CLI 진입점 ───────────────────────────────────────────────────────────────
#
# 골든셋(samples/questions.jsonl) + registry(registry/)를 로드해 분류기를 골라(`rule`은
# 결정론 데모 키워드, `llm`은 실 `claude -p` Haiku·게이트 밖) eval을 돌리고 EvalReport를
# 출력한다. ADR 0015 단일 출처: 분류 정확도용 classifier와 Router 내장 classifier를 *같은
# 인스턴스*로 둬야 라우팅 정확도와 분류 정확도가 일관(Router가 내부에서 같은 classify 결과로
# 라우팅) — _build_router_and_classifier가 한 인스턴스를 만들어 둘 다에 쓴다.

ROOT_USER = "root_manager"


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent.parent


def _build_classifier(kind: str, intents: list[str]) -> Classifier:
    """`--classifier` 선택을 실제 Classifier로 만든다.

    - `rule`: RuleBasedClassifier(데모 키워드, `demo.demo_keyword_intents()` 재사용) — 결정론·
      실 LLM 0이라 CLI 빠른 확인용 — 단 *라우팅 가능한* 골든 intent만 덮는다(unowned gap
      intent·자연어 표현 질문은 키워드 미수록이라 정확도가 낮게 나옴, 진짜 정확도는 llm로 본다).
    - `llm`: LlmClassifier(intents 주입, runner=None → 실 `claude -p` Haiku 지연 바인딩).
      실 LLM·비결정·게이트 밖 — 분류 품질 측정이 목적(수동/야간).
    """
    if kind == "rule":
        from agent_org_network.classifier import RuleBasedClassifier
        from agent_org_network.demo import demo_keyword_intents

        return RuleBasedClassifier(demo_keyword_intents())
    if kind == "llm":
        from agent_org_network.classifier import LlmClassifier

        return LlmClassifier(intents)
    raise ValueError(f"알 수 없는 분류기 종류: {kind!r} (rule|llm)")


def main(argv: list[str] | None = None) -> int:
    """골든셋 eval CLI: registry·골든셋 로드 → 분류기 선택 → run_eval → EvalReport 출력.

    `python -m agent_org_network.eval [--classifier rule|llm] [--threshold 0.8]`.
    종료 코드는 통과(0)/실패(1) — 야간 회귀 게이트가 종료 코드로 통과 여부를 본다.
    """
    import argparse
    from pathlib import Path

    from agent_org_network.golden import load_golden
    from agent_org_network.registry import Registry

    parser = argparse.ArgumentParser(
        prog="python -m agent_org_network.eval",
        description="골든셋으로 분류·라우팅 정확도를 재고 임계 통과 여부를 출력한다.",
    )
    parser.add_argument(
        "--classifier",
        choices=("rule", "llm"),
        default="rule",
        help="분류기 종류 (rule=데모 키워드·결정론·기본, llm=실 claude -p Haiku·게이트 밖)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_ACCURACY_THRESHOLD,
        help=f"통과 임계(분류·라우팅 둘 다 이 비율 이상이어야 통과, 기본 {DEFAULT_ACCURACY_THRESHOLD})",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=_repo_root() / "registry",
        help="registry 디렉터리(users.yaml + agents/*.yaml)",
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=_repo_root() / "samples" / "questions.jsonl",
        help="골든셋 JSONL 경로",
    )
    args = parser.parse_args(argv)

    registry = Registry()
    registry.load(args.registry)
    registry.validate()
    samples = load_golden(args.golden)

    # intents = registry 카드 domains의 합집합(LlmClassifier 어휘). 결정론 정렬로 고정.
    intents = sorted({d for c in registry.all_cards() for d in c.domains})

    # ADR 0015: 분류 정확도(run_eval가 직접 classify)와 라우팅 정확도(Router 내장 classify)가
    # *같은 분류기 인스턴스*를 봐야 일관 — 한 번 만들어 Router·run_eval에 같이 넘긴다.
    classifier = _build_classifier(args.classifier, intents)
    router = Router(registry, classifier, root_user=ROOT_USER)

    # ADR 0041 갭 라벨 채점 보정: 골든셋 unowned 갭 질문의 expected_intent가 registry
    # 어휘(intents) 밖이라, 어휘 사영으로 분류 축 채점을 보정(rule/llm 공통 적용).
    report = run_eval(samples, classifier, router, threshold=args.threshold, vocabulary=intents)

    print(f"[eval] 분류기={args.classifier} 골든셋={report.total}개 임계={report.threshold}")
    print(f"  분류 정확도: {report.classification_accuracy:.3f}")
    print(f"  라우팅 정확도: {report.routing_accuracy:.3f}")
    print(f"  통과: {'PASS' if report.passed else 'FAIL'}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
