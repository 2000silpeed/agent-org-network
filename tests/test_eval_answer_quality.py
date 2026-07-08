"""큐레이션 골든셋 — 답변 품질 축 + 라벨 provenance 결정론 테스트(ADR 0041, Phase 15 B축).

B1(golden.py 스키마 확장: `CurationProvenance`·`AnswerExpectation`·`SampleQuestion.answer_expectation`)·
B2(eval.py 러너 확장: `AnswerGrader`+`FakeGrader`/`SubstringGrader`/`LlmGrader`·`run_eval`
답변축 재생)·B3(라우팅 검증 재사용 + 자동라벨 금지 구조 가드)를 덮는다.

실 LLM·네트워크·실 큐레이션 0 — `StubRuntime` 동형의 고정 응답 fake runtime + `FakeGrader`/
`SubstringGrader`(결정론)만 돈다. 실 `LlmGrader`는 게이트 밖(NotImplementedError 자리).
"""

from __future__ import annotations

import ast
import inspect
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

from agent_org_network.agent_card import AgentCard
from agent_org_network.classifier import Classifier, FakeClassifier
from agent_org_network.decision import Routed
from agent_org_network import eval as eval_module
from agent_org_network import golden as golden_module
from agent_org_network.eval import (
    DEFAULT_ACCURACY_THRESHOLD,
    AnswerGrader,
    EvalReport,
    FakeGrader,
    Grade,
    LlmGrader,
    SubstringGrader,
    run_eval,
)
from agent_org_network.golden import (
    AnswerExpectation,
    CurationProvenance,
    SampleQuestion,
    load_golden,
)
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import Answer, AgentRuntime
from agent_org_network.user import User

REPO_ROOT = Path(__file__).resolve().parents[1]
QUESTIONS_PATH = REPO_ROOT / "samples" / "questions.jsonl"
ROOT_USER = "root_manager"

_PROVENANCE = CurationProvenance(curated_by="alice", source_hint="mail")


def _card(agent_id: str, owner: str, domains: list[str]) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="t",
        summary="s",
        domains=domains,
        last_reviewed_at=date(2026, 6, 20),
    )


def _routed_registry() -> Registry:
    """환불 → cs_ops 단일(routed) 레지스트리."""
    reg = Registry()
    reg.register_user(User(id=ROOT_USER))
    reg.register_user(User(id="o1", manager=ROOT_USER))
    reg.register(_card("cs_ops", "o1", ["환불"]))
    reg.validate()
    return reg


def _contested_registry() -> Registry:
    """보상 → cs_ops·finance_ops 둘(contested) 레지스트리."""
    reg = Registry()
    reg.register_user(User(id=ROOT_USER))
    reg.register_user(User(id="o1", manager=ROOT_USER))
    reg.register_user(User(id="o2", manager=ROOT_USER))
    reg.register(_card("cs_ops", "o1", ["보상"]))
    reg.register(_card("finance_ops", "o2", ["보상"]))
    reg.validate()
    return reg


class _FixedRuntime:
    """고정 텍스트만 돌려주는 결정론 `AgentRuntime` — 호출 관측(calls)까지 제공."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[str, str]] = []

    def answer(
        self,
        question: str,
        card: AgentCard,
        context: str | None = None,
        grounding: str | None = None,
    ) -> Answer:
        self.calls.append((question, card.agent_id))
        return Answer(text=self.text, sources=(), mode="full")


class _OracleClassifier:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def classify(self, question: str) -> str:
        return self._mapping.get(question, "")


def _oracle(samples: list[SampleQuestion]) -> _OracleClassifier:
    return _OracleClassifier({s.question: s.expected_intent for s in samples})


def _run(
    samples: list[SampleQuestion],
    registry: Registry,
    classifier: Classifier,
    *,
    runtime: AgentRuntime | None = None,
    grader: AnswerGrader | None = None,
    threshold: float = DEFAULT_ACCURACY_THRESHOLD,
) -> EvalReport:
    router = Router(registry, classifier, root_user=ROOT_USER)
    return run_eval(
        samples, classifier, router, threshold=threshold, runtime=runtime, grader=grader
    )


# ── B1: golden.py 스키마 확장 ─────────────────────────────────────────────────


def test_CurationProvenance_curated_by_빈값_거부() -> None:
    with pytest.raises(ValidationError):
        CurationProvenance(curated_by="")


def test_CurationProvenance_curated_by_공백만_거부() -> None:
    with pytest.raises(ValidationError):
        CurationProvenance(curated_by="   ")


def test_CurationProvenance_source_hint_기본값_빈문자열() -> None:
    prov = CurationProvenance(curated_by="alice")
    assert prov.source_hint == ""


def test_AnswerExpectation_provenance_없이_구성_불가() -> None:
    with pytest.raises(ValidationError):
        AnswerExpectation(criteria=("환불",))  # pyright: ignore[reportCallIssue]


def test_AnswerExpectation_provenance_있으면_구성_성공() -> None:
    expectation = AnswerExpectation(criteria=("환불",), provenance=_PROVENANCE)
    assert expectation.match == "all"
    assert expectation.provenance.curated_by == "alice"


def test_기존_JSONL_answer_expectation_없이_로드_성공() -> None:
    """기존 samples/questions.jsonl(30줄·answer_expectation 없음)이 하위호환으로 로드된다."""
    entries = load_golden(QUESTIONS_PATH)
    assert len(entries) == 30
    assert all(entry.answer_expectation is None for entry in entries)


def test_SampleQuestion_answer_expectation_중첩_검증_성공() -> None:
    raw = {
        "question": "환불 절차 알려줘",
        "expected_intent": "환불",
        "expected_disposition": "routed",
        "expected_primary": "cs_ops",
        "answer_expectation": {
            "criteria": ["환불", "정책"],
            "match": "all",
            "provenance": {"curated_by": "alice", "source_hint": "mail"},
        },
    }
    sample = SampleQuestion.model_validate(raw)
    assert sample.answer_expectation is not None
    assert sample.answer_expectation.criteria == ("환불", "정책")
    assert sample.answer_expectation.provenance.curated_by == "alice"


def test_SampleQuestion_answer_expectation_provenance_없으면_검증_실패() -> None:
    raw = {
        "question": "환불 절차 알려줘",
        "expected_intent": "환불",
        "expected_disposition": "routed",
        "expected_primary": "cs_ops",
        "answer_expectation": {"criteria": ["환불"]},
    }
    with pytest.raises(ValidationError):
        SampleQuestion.model_validate(raw)


# ── B2: eval.py 러너 확장 ────────────────────────────────────────────────────


def test_FakeGrader_True면_answer_quality_accuracy_1점0() -> None:
    sample = SampleQuestion(
        question="환불 절차 알려줘",
        expected_intent="환불",
        expected_disposition="routed",
        expected_primary="cs_ops",
        answer_expectation=AnswerExpectation(criteria=("환불",), provenance=_PROVENANCE),
    )
    samples = [sample]
    runtime = _FixedRuntime(text="아무 답")
    report = _run(
        samples, _routed_registry(), _oracle(samples), runtime=runtime, grader=FakeGrader(True)
    )
    assert report.answer_quality_accuracy == 1.0
    assert report.answer_graded == 1
    assert runtime.calls == [("환불 절차 알려줘", "cs_ops")]


def test_FakeGrader_False면_answer_quality_accuracy_0점0_및_passed_False() -> None:
    sample = SampleQuestion(
        question="환불 절차 알려줘",
        expected_intent="환불",
        expected_disposition="routed",
        expected_primary="cs_ops",
        answer_expectation=AnswerExpectation(criteria=("환불",), provenance=_PROVENANCE),
    )
    samples = [sample]
    report = _run(
        samples,
        _routed_registry(),
        _oracle(samples),
        runtime=_FixedRuntime(text="아무 답"),
        grader=FakeGrader(False),
    )
    assert report.answer_quality_accuracy == 0.0
    # 분류·라우팅은 통과선이어도 답변 품질 미달이면 전체 실패(AND 조건 확장).
    assert report.classification_accuracy == 1.0
    assert report.routing_accuracy == 1.0
    assert report.passed is False


def test_SubstringGrader_criteria_전부_포함시_통과_match_all() -> None:
    sample = SampleQuestion(
        question="환불 절차 알려줘",
        expected_intent="환불",
        expected_disposition="routed",
        expected_primary="cs_ops",
        answer_expectation=AnswerExpectation(
            criteria=("환불", "정책"), match="all", provenance=_PROVENANCE
        ),
    )
    samples = [sample]
    report = _run(
        samples,
        _routed_registry(),
        _oracle(samples),
        runtime=_FixedRuntime(text="환불 정책은 다음과 같습니다."),
        grader=SubstringGrader(),
    )
    assert report.answer_quality_accuracy == 1.0


def test_SubstringGrader_criteria_일부_불포함시_실패_match_all() -> None:
    sample = SampleQuestion(
        question="환불 절차 알려줘",
        expected_intent="환불",
        expected_disposition="routed",
        expected_primary="cs_ops",
        answer_expectation=AnswerExpectation(
            criteria=("환불", "정책"), match="all", provenance=_PROVENANCE
        ),
    )
    samples = [sample]
    report = _run(
        samples,
        _routed_registry(),
        _oracle(samples),
        runtime=_FixedRuntime(text="환불은 가능합니다."),  # "정책" 없음
        grader=SubstringGrader(),
    )
    assert report.answer_quality_accuracy == 0.0


def test_SubstringGrader_match_any_하나만_포함해도_통과() -> None:
    sample = SampleQuestion(
        question="환불 절차 알려줘",
        expected_intent="환불",
        expected_disposition="routed",
        expected_primary="cs_ops",
        answer_expectation=AnswerExpectation(
            criteria=("환불", "교환"), match="any", provenance=_PROVENANCE
        ),
    )
    samples = [sample]
    report = _run(
        samples,
        _routed_registry(),
        _oracle(samples),
        runtime=_FixedRuntime(text="교환은 가능합니다."),  # "환불" 없지만 "교환" 있음
        grader=SubstringGrader(),
    )
    assert report.answer_quality_accuracy == 1.0


def test_SubstringGrader_match_any_아무것도_포함_안하면_실패() -> None:
    sample = SampleQuestion(
        question="환불 절차 알려줘",
        expected_intent="환불",
        expected_disposition="routed",
        expected_primary="cs_ops",
        answer_expectation=AnswerExpectation(
            criteria=("환불", "교환"), match="any", provenance=_PROVENANCE
        ),
    )
    samples = [sample]
    report = _run(
        samples,
        _routed_registry(),
        _oracle(samples),
        runtime=_FixedRuntime(text="잘 모르겠습니다."),
        grader=SubstringGrader(),
    )
    assert report.answer_quality_accuracy == 0.0


def test_SubstringGrader_빈_criteria_match_all은_공허하게_통과() -> None:
    """docstring 계약: 빈 criteria + match="all"은 포함해야 할 게 없어 공허하게 통과."""
    grade = SubstringGrader().grade(
        Answer(text="아무 텍스트"),
        AnswerExpectation(criteria=(), match="all", provenance=_PROVENANCE),
        "질문",
    )
    assert grade.passed is True


def test_SubstringGrader_빈_criteria_match_any는_통과할_게_없어_실패() -> None:
    """docstring 계약: 빈 criteria + match="any"는 통과할 게 없어 실패로 취급."""
    grade = SubstringGrader().grade(
        Answer(text="아무 텍스트"),
        AnswerExpectation(criteria=(), match="any", provenance=_PROVENANCE),
        "질문",
    )
    assert grade.passed is False


def test_answer_expectation_없는_샘플은_answer_graded_0이고_accuracy_None() -> None:
    sample = SampleQuestion(
        question="환불 절차 알려줘",
        expected_intent="환불",
        expected_disposition="routed",
        expected_primary="cs_ops",
    )
    samples = [sample]
    report = _run(
        samples,
        _routed_registry(),
        _oracle(samples),
        runtime=_FixedRuntime(text="x"),
        grader=FakeGrader(True),
    )
    assert report.answer_graded == 0
    assert report.answer_quality_accuracy is None
    assert report.passed is True  # None은 미달이 아니다


def test_runtime와_grader_미주입시_답변축_skip_기존_리포트_무변경() -> None:
    sample = SampleQuestion(
        question="환불 절차 알려줘",
        expected_intent="환불",
        expected_disposition="routed",
        expected_primary="cs_ops",
        answer_expectation=AnswerExpectation(criteria=("환불",), provenance=_PROVENANCE),
    )
    samples = [sample]
    report = _run(samples, _routed_registry(), _oracle(samples))  # runtime·grader 둘 다 None
    assert report.answer_graded == 0
    assert report.answer_quality_accuracy is None
    assert report.classification_accuracy == 1.0
    assert report.routing_accuracy == 1.0
    assert report.passed is True


def test_grader만_주입되고_runtime_없으면_답변축_skip() -> None:
    sample = SampleQuestion(
        question="환불 절차 알려줘",
        expected_intent="환불",
        expected_disposition="routed",
        expected_primary="cs_ops",
        answer_expectation=AnswerExpectation(criteria=("환불",), provenance=_PROVENANCE),
    )
    samples = [sample]
    report = _run(samples, _routed_registry(), _oracle(samples), grader=FakeGrader(True))
    assert report.answer_graded == 0
    assert report.answer_quality_accuracy is None


def test_Contested는_answer_expectation_있어도_채점_스코프_밖() -> None:
    sample = SampleQuestion(
        question="보상 기준이 뭐야",
        expected_intent="보상",
        expected_disposition="contested",
        expected_candidates=["cs_ops", "finance_ops"],
        answer_expectation=AnswerExpectation(criteria=("보상",), provenance=_PROVENANCE),
    )
    samples = [sample]
    runtime = _FixedRuntime(text="보상 안내")
    report = _run(
        samples,
        _contested_registry(),
        FakeClassifier("보상"),
        runtime=runtime,
        grader=FakeGrader(True),
    )
    assert report.answer_graded == 0
    assert report.answer_quality_accuracy is None
    assert runtime.calls == []


def test_Unowned는_answer_expectation_있어도_채점_스코프_밖() -> None:
    sample = SampleQuestion(
        question="주차권 갱신은 어디서",
        expected_intent="주차",
        expected_disposition="unowned",
        answer_expectation=AnswerExpectation(criteria=("주차",), provenance=_PROVENANCE),
    )
    samples = [sample]
    runtime = _FixedRuntime(text="주차 안내")
    report = _run(
        samples,
        _routed_registry(),
        FakeClassifier("주차"),
        runtime=runtime,
        grader=FakeGrader(True),
    )
    assert report.answer_graded == 0
    assert report.answer_quality_accuracy is None
    assert runtime.calls == []


def test_LlmGrader_grade는_NotImplementedError() -> None:
    with pytest.raises(NotImplementedError):
        LlmGrader().grade(
            Answer(text="x"),
            AnswerExpectation(criteria=("x",), provenance=_PROVENANCE),
            "질문",
        )


def test_Grade_필드() -> None:
    grade = Grade(passed=True, reason="근거")
    assert grade.passed is True
    assert grade.reason == "근거"


# ── B3: 라우팅 검증 재사용 + 자동라벨 금지 구조 가드 ──────────────────────────


def test_routing_matches는_기존_함수_1개만_존재한다() -> None:
    """`expected_primary` 매칭 로직은 기존 `_routing_matches` 재사용(신규 중복 함수 0)."""
    source = inspect.getsource(eval_module)
    tree = ast.parse(source)
    defs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_routing_matches"
    ]
    assert len(defs) == 1


def _imported_module_names(module: ModuleType) -> set[str]:
    """모듈 소스의 import 구문(만) 대상 모듈명 집합 — docstring/주석 언급은 무시한다."""
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _imported_symbol_names(module: ModuleType) -> set[str]:
    """모듈 소스의 `from X import a, b` 구문에서 가져온 심볼명 집합(import 구문만)."""
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


def test_golden_모듈은_runtime_Answer를_import하지_않는다() -> None:
    """golden.py가 `agent_org_network.runtime`도 `Answer` 심볼도 import하지 않는다.

    (docstring/주석의 언급이 아니라 실제 import 구문만 본다 — 구조 가드는 실행 가능한
    의존을 막는 것이지 산문 설명을 검열하는 게 아니다.) `from agent_org_network import
    runtime`처럼 module="agent_org_network"·심볼="runtime"인 우회도 잡도록 심볼 집합에도
    "runtime"을 금지어로 검사한다(module 경로만 보면 이 형태를 놓친다).
    """
    imported_modules = _imported_module_names(golden_module)
    assert not any(m == "agent_org_network.runtime" or m.endswith(".runtime") for m in imported_modules)
    imported_symbols = _imported_symbol_names(golden_module)
    assert "Answer" not in imported_symbols
    assert "runtime" not in imported_symbols


def test_golden_모듈은_mail_모듈을_import하지_않는다() -> None:
    imported_modules = _imported_module_names(golden_module)
    assert not any("mail" in m for m in imported_modules)


def test_eval_모듈은_mail_모듈을_import하지_않는다() -> None:
    imported_modules = _imported_module_names(eval_module)
    assert not any("mail" in m for m in imported_modules)


def test_golden_모듈에_Answer로부터_SampleQuestion_또는_라벨을_파생하는_함수가_없다() -> None:
    """`Answer`를 인자로 받는 함수·`derive`/`from_answer`류 이름의 함수가 golden.py에 없다.

    위치인자(`args`)만 보면 키워드전용(`kwonlyargs`)·위치전용(`posonlyargs`)·`*args`/
    `**kwargs`로 `Answer` 타입을 받는 우회를 놓친다 — 모든 인자 종류를 순회한다. 또한
    `async def`(`ast.AsyncFunctionDef`)도 `FunctionDef`와 동형이라 검사 대상에 포함한다.
    """
    tree = ast.parse(inspect.getsource(golden_module))
    forbidden_name_markers = ("derive", "from_answer", "answer_to_")
    function_node_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, function_node_types):
            continue
        lowered_name = node.name.lower()
        assert not any(marker in lowered_name for marker in forbidden_name_markers), (
            f"라벨 자동 파생 의심 함수명: {node.name}"
        )
        all_args = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if node.args.vararg is not None:
            all_args.append(node.args.vararg)
        if node.args.kwarg is not None:
            all_args.append(node.args.kwarg)
        for arg in all_args:
            annotation = arg.annotation
            if annotation is None:
                continue
            annotation_text = ast.unparse(annotation)
            assert "Answer" not in annotation_text.replace("AnswerExpectation", ""), (
                f"{node.name}({arg.arg}: {annotation_text}) — Answer 타입을 인자로 받는 함수 금지"
            )


def test_기존_골든셋_라우팅_coherence_무회귀() -> None:
    """B축 확장이 기존 라우팅 정확도 계산(①②)에 회귀를 만들지 않는다."""
    sample = SampleQuestion(
        question="환불 절차 알려줘",
        expected_intent="환불",
        expected_disposition="routed",
        expected_primary="cs_ops",
    )
    registry = _routed_registry()
    router = Router(registry, FakeClassifier("환불"), root_user=ROOT_USER)
    decision = router.route(sample.question)
    assert isinstance(decision, Routed)
    assert decision.primary.agent_id == "cs_ops"
    report = run_eval([sample], FakeClassifier("환불"), router)
    assert report.classification_accuracy == 1.0
    assert report.routing_accuracy == 1.0
    assert report.answer_graded == 0
    assert report.answer_quality_accuracy is None
