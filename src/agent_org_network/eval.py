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

결정론 vs 실 LLM 경계(ADR 0003):
  - **러너 *구조*는 결정론 테스트**다 — 주입한 분류기가 expected/wrong intent를 내면
    정확도 계산·임계 통과/실패가 맞나를 `FakeClassifier`로 검증(tdd-engineer). 러너가
    골든셋을 받아 메트릭을 집계하고 임계와 비교하는 *로직*은 분류기와 무관하게 결정론.
  - **실 `LlmClassifier`로 도는 정확도 측정은 게이트 밖**(eval/수동·야간 — 실 LLM,
    TRD §7). CI 매 커밋 게이트가 아니라 분류기 변경 시·야간 회귀로 돈다.

CLI 진입점 `main`(`python -m agent_org_network.eval`) — 골든셋 경로·분류기 선택(rule/llm)·
임계값을 인자로 받아 EvalReport를 출력하고 통과/실패를 종료 코드로 돌려준다.

범위 밖(자리만): 임베딩 유사도·다중 LLM·프롬프트 튜닝·캐싱·confusion matrix 시각화.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_org_network.classifier import Classifier
from agent_org_network.decision import Contested, RoutingDecision, Routed, Unowned
from agent_org_network.golden import SampleQuestion
from agent_org_network.router import Router

# 기본 통과 임계(예시) — 분류·라우팅 정확도가 이 비율 미만이면 실패. 실 값·이전 대비
# 하락 판정은 구현/운영에서 확정(CONTEXT Golden set "임계 미달·이전 대비 하락이 실패").
DEFAULT_ACCURACY_THRESHOLD = 0.8


@dataclass(frozen=True)
class EvalReport:
    """골든셋 eval 1회의 결과 — 두 정확도와 임계 통과 여부.

    frozen 값 객체로 메트릭을 안는다(러너가 산출). `classification_accuracy`(①)·
    `routing_accuracy`(②)·`total`(평가한 질문 수)·`threshold`(적용 임계)·`passed`(둘 다
    임계 이상인가). 한 예제 틀림이 아니라 *집계 비율 vs 임계*가 통과를 가른다.
    개별 미스(어떤 질문이 틀렸나)는 디버깅용 후속 필드 — MVP는 집계 비율·통과 여부만.
    """

    classification_accuracy: float
    routing_accuracy: float
    total: int
    threshold: float
    passed: bool


def run_eval(
    samples: list[SampleQuestion],
    classifier: Classifier,
    router: Router,
    threshold: float = DEFAULT_ACCURACY_THRESHOLD,
) -> EvalReport:
    """골든셋에 대해 분류·라우팅 정확도를 재고 임계와 비교한 EvalReport를 돌려준다.

    각 SampleQuestion에 대해 ① classify(question)==expected_intent(분류 정확도)와
    ② route(question) disposition/primary/candidates==expected(라우팅 정확도)를 집계해
    두 비율을 내고, 둘 다 threshold 이상이면 passed=True. classifier를 *직접* 부르는 게
    ①의 핵심(분류 품질 측정 — route의 단일 intent와 별도). router는 ②를 위해 받는다.

    결정론 테스트: FakeClassifier(expected 또는 wrong intent)를 주입하면 정확도·통과/실패가
    예측대로 나오는지 검증한다(러너 구조 결정론). 실 LlmClassifier 주입은 eval/수동.
    """
    total = len(samples)
    if total == 0:
        return EvalReport(
            classification_accuracy=0.0,
            routing_accuracy=0.0,
            total=0,
            threshold=threshold,
            passed=False,
        )

    classification_hits = 0
    routing_hits = 0
    for sample in samples:
        if classifier.classify(sample.question) == sample.expected_intent:
            classification_hits += 1
        if _routing_matches(router.route(sample.question), sample):
            routing_hits += 1

    classification_accuracy = classification_hits / total
    routing_accuracy = routing_hits / total
    passed = classification_accuracy >= threshold and routing_accuracy >= threshold
    return EvalReport(
        classification_accuracy=classification_accuracy,
        routing_accuracy=routing_accuracy,
        total=total,
        threshold=threshold,
        passed=passed,
    )


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

    report = run_eval(samples, classifier, router, threshold=args.threshold)

    print(f"[eval] 분류기={args.classifier} 골든셋={report.total}개 임계={report.threshold}")
    print(f"  분류 정확도: {report.classification_accuracy:.3f}")
    print(f"  라우팅 정확도: {report.routing_accuracy:.3f}")
    print(f"  통과: {'PASS' if report.passed else 'FAIL'}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
