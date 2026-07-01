"""T6.4 골든셋 결정론 테스트.

1. Registry.load(registry/) + validate() 통과 — 5장 카드·6명 유저.
2. 골든셋 well-formed — 30개 로드·필수 필드·실재 agent_id·disposition coherence.
3. 라벨↔라우터 coherence(핵심) — FakeClassifier 주입, 실 registry 기준 라우팅 일치.
   급여이체 케이스가 Unowned → [0] cannot_answer 차감 수정을 구동.

실 Claude·LLM·네트워크 0. registry/·samples/ 읽기 전용.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_org_network.classifier import FakeClassifier
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.golden import SampleQuestion, load_golden
from agent_org_network.registry import Registry
from agent_org_network.router import Router

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_DIR = REPO_ROOT / "registry"
QUESTIONS_PATH = REPO_ROOT / "samples" / "questions.jsonl"
ROOT_USER = "root_manager"


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = Registry()
    reg.load(REGISTRY_DIR)
    reg.validate()
    return reg


@pytest.fixture(scope="module")
def golden() -> list[SampleQuestion]:
    return load_golden(QUESTIONS_PATH)


# ── 1. 카드 등록 ────────────────────────────────────────────────────────────

def test_registry_로드_후_카드_5장_유저_6명():
    reg = Registry()
    reg.load(REGISTRY_DIR)
    reg.validate()
    assert len(reg.all_cards()) == 5
    assert len(reg.user_ids()) == 6


def test_registry_5장_카드_agent_id_집합():
    reg = Registry()
    reg.load(REGISTRY_DIR)
    expected = {"contract_ops", "cs_ops", "finance_ops", "hr_ops", "it_ops"}
    assert {c.agent_id for c in reg.all_cards()} == expected


def test_registry_신규_카드_hr_ops_cannot_answer():
    reg = Registry()
    reg.load(REGISTRY_DIR)
    hr = reg.get("hr_ops")
    assert "급여이체" in hr.domains
    assert "급여이체" in hr.cannot_answer


def test_registry_신규_카드_hr_ops_approval_when():
    reg = Registry()
    reg.load(REGISTRY_DIR)
    hr = reg.get("hr_ops")
    assert "평가" in hr.approval_when


def test_registry_신규_카드_it_ops_approval_when():
    reg = Registry()
    reg.load(REGISTRY_DIR)
    it = reg.get("it_ops")
    assert "접근권한" in it.approval_when


# ── 2. 골든셋 well-formed ────────────────────────────────────────────────────

def test_골든셋_30개_로드(golden: list[SampleQuestion]):
    assert len(golden) == 30


def test_골든셋_tier_없는_기존_항목_기본값_easy(golden: list[SampleQuestion]):
    """기존 samples/questions.jsonl(30줄·tier 없음)은 SampleQuestion.tier 기본값 "easy"로 통과한다(무회귀)."""
    for entry in golden:
        assert entry.tier == "easy", f"tier 기본값 불일치: {entry}"


def test_SampleQuestion_tier_유효값_hard_ambiguous_허용():
    for tier in ("easy", "hard", "ambiguous"):
        q = SampleQuestion(
            question="q", expected_intent="i", expected_disposition="unowned", tier=tier
        )
        assert q.tier == tier


def test_SampleQuestion_tier_잘못된_값_검증_실패():
    with pytest.raises(ValidationError):
        SampleQuestion(
            question="q",
            expected_intent="i",
            expected_disposition="unowned",
            tier="bogus",  # pyright: ignore[reportArgumentType]  # 검증 실패를 확인하는 의도적 잘못된 값
        )


def test_골든셋_모든_항목_필수_필드_존재(golden: list[SampleQuestion]):
    for entry in golden:
        assert entry.question, f"question 빈 값: {entry}"
        assert entry.expected_intent is not None, f"expected_intent 없음: {entry}"
        assert entry.expected_disposition in ("routed", "contested", "unowned"), (
            f"disposition 범위 오류: {entry.expected_disposition}"
        )


def test_골든셋_routed_항목_primary_실재(golden: list[SampleQuestion], registry: Registry):
    all_ids = {c.agent_id for c in registry.all_cards()}
    for entry in golden:
        if entry.expected_disposition == "routed":
            assert entry.expected_primary is not None, f"routed인데 primary 없음: {entry.question}"
            assert entry.expected_primary in all_ids, (
                f"primary '{entry.expected_primary}'가 registry에 없음: {entry.question}"
            )


def test_골든셋_contested_항목_candidates_실재(golden: list[SampleQuestion], registry: Registry):
    all_ids = {c.agent_id for c in registry.all_cards()}
    for entry in golden:
        if entry.expected_disposition == "contested":
            assert entry.expected_candidates is not None, (
                f"contested인데 candidates 없음: {entry.question}"
            )
            assert len(entry.expected_candidates) >= 2, (
                f"contested candidates < 2: {entry.question}"
            )
            for cid in entry.expected_candidates:
                assert cid in all_ids, f"candidate '{cid}'가 registry에 없음: {entry.question}"


def test_골든셋_unowned_항목_primary_없음(golden: list[SampleQuestion]):
    for entry in golden:
        if entry.expected_disposition == "unowned":
            assert entry.expected_primary is None, (
                f"unowned인데 primary 있음: {entry.question}"
            )


def test_골든셋_disposition_coherence_routed(golden: list[SampleQuestion], registry: Registry):
    """routed → cannot_answer 차감 후 후보가 정확히 1개이고 expected_primary와 일치."""
    cards = registry.all_cards()
    for entry in golden:
        if entry.expected_disposition != "routed":
            continue
        intent = entry.expected_intent
        candidates = [
            c for c in cards
            if intent in c.domains and intent not in c.cannot_answer
        ]
        assert len(candidates) == 1, (
            f"routed인데 후보 {len(candidates)}개: intent='{intent}' question='{entry.question}'"
        )
        assert candidates[0].agent_id == entry.expected_primary, (
            f"primary 불일치: 기대={entry.expected_primary} 실제={candidates[0].agent_id}"
        )


def test_골든셋_disposition_coherence_contested(golden: list[SampleQuestion], registry: Registry):
    """contested → cannot_answer 차감 후 후보 ≥ 2이고 집합이 expected_candidates와 일치."""
    cards = registry.all_cards()
    for entry in golden:
        if entry.expected_disposition != "contested":
            continue
        intent = entry.expected_intent
        candidates = [
            c for c in cards
            if intent in c.domains and intent not in c.cannot_answer
        ]
        assert len(candidates) >= 2, (
            f"contested인데 후보 {len(candidates)}개: intent='{intent}'"
        )
        assert {c.agent_id for c in candidates} == set(entry.expected_candidates or []), (
            f"contested candidates 불일치: intent='{intent}'"
        )


def test_골든셋_disposition_coherence_unowned(golden: list[SampleQuestion], registry: Registry):
    """unowned → cannot_answer 차감 후 후보가 0개."""
    cards = registry.all_cards()
    for entry in golden:
        if entry.expected_disposition != "unowned":
            continue
        intent = entry.expected_intent
        candidates = [
            c for c in cards
            if intent in c.domains and intent not in c.cannot_answer
        ]
        assert len(candidates) == 0, (
            f"unowned인데 후보 {len(candidates)}개: intent='{intent}' question='{entry.question}'"
        )


# ── 3. 라벨↔라우터 coherence (핵심) ─────────────────────────────────────────

def test_골든셋_라우터_coherence_전체(golden: list[SampleQuestion], registry: Registry):
    """각 질문에 FakeClassifier(expected_intent)를 주입해 Router 결정과 골든 라벨을 비교."""
    for entry in golden:
        router = Router(
            registry,
            FakeClassifier(entry.expected_intent),
            root_user=ROOT_USER,
            precedents=None,
        )
        decision = router.route(entry.question)
        disposition = entry.expected_disposition

        if disposition == "routed":
            assert isinstance(decision, Routed), (
                f"[{entry.question}] 기대=routed 실제={type(decision).__name__}"
            )
            assert decision.primary.agent_id == entry.expected_primary, (
                f"[{entry.question}] primary 불일치: 기대={entry.expected_primary} "
                f"실제={decision.primary.agent_id}"
            )
            assert decision.requires_approval == entry.expected_approval, (
                f"[{entry.question}] requires_approval 불일치: "
                f"기대={entry.expected_approval} 실제={decision.requires_approval}"
            )

        elif disposition == "contested":
            assert isinstance(decision, Contested), (
                f"[{entry.question}] 기대=contested 실제={type(decision).__name__}"
            )
            actual_ids = {c.agent_id for c in decision.candidates}
            assert actual_ids == set(entry.expected_candidates or []), (
                f"[{entry.question}] contested candidates 불일치: "
                f"기대={entry.expected_candidates} 실제={sorted(actual_ids)}"
            )

        elif disposition == "unowned":
            assert isinstance(decision, Unowned), (
                f"[{entry.question}] 기대=unowned 실제={type(decision).__name__}"
            )
            assert decision.escalated_to == ROOT_USER, (
                f"[{entry.question}] escalated_to 불일치: "
                f"기대={ROOT_USER} 실제={decision.escalated_to}"
            )


def test_급여이체_cannot_answer_차감으로_Unowned(registry: Registry):
    """cannot_answer 차감 수정 구동 케이스.

    급여이체는 hr_ops.domains에 있지만 hr_ops.cannot_answer에도 있어
    차감 후 후보 0 → Unowned. 수정 전이면 Routed(hr_ops)가 돼 실패한다.
    """
    router = Router(registry, FakeClassifier("급여이체"), root_user=ROOT_USER)
    decision = router.route("이번 달 급여이체를 실행해줄 수 있나요?")
    assert isinstance(decision, Unowned), (
        f"급여이체는 cannot_answer 차감으로 Unowned여야 함. 실제: {type(decision).__name__}"
    )
    assert decision.escalated_to == ROOT_USER


def test_골든셋_분포_routed_20건_이상(golden: list[SampleQuestion]):
    routed = [e for e in golden if e.expected_disposition == "routed"]
    assert len(routed) >= 20, f"Routed {len(routed)}건 (기대 ≥ 20)"


def test_골든셋_분포_contested_5건_이상(golden: list[SampleQuestion]):
    contested = [e for e in golden if e.expected_disposition == "contested"]
    assert len(contested) >= 5, f"Contested {len(contested)}건 (기대 ≥ 5)"


def test_골든셋_분포_unowned_5건_이상(golden: list[SampleQuestion]):
    unowned = [e for e in golden if e.expected_disposition == "unowned"]
    assert len(unowned) >= 5, f"Unowned {len(unowned)}건 (기대 ≥ 5)"


def test_골든셋_approval_true_케이스_존재(golden: list[SampleQuestion]):
    approval_cases = [e for e in golden if e.expected_approval]
    assert len(approval_cases) >= 2, f"approval=True 케이스 {len(approval_cases)}건 (기대 ≥ 2)"
