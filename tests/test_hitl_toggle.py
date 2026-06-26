"""T9.3(a) — HITL 토글 상태 + →mode 매핑 순수 로직 단위 테스트 (ADR 0025).

불변식:
  - mode는 원래 노출하는 신뢰 상태값(Answer 절) — HITL 토글이 값을 바꿔도 노출 경계 그대로.
  - 전이≠기록: 토글 변경은 운영 설정 set이지 도메인 전이가 아님.
  - 토글 변경이 라우팅 종착을 안 바꿈(답 mode만 바꿈).
  - 기존 _apply_approval_gate 회귀 0.
  - under-claim 단조성: 카드 approval_when이 설정된 에이전트는 HITL off여도 draft_only 유지.
  - 운영자 HITL on은 상향만 가능(full→draft_only). backup은 덮지 않음.
"""

from agent_org_network.hitl import (
    HitlToggleMap,
    hitl_to_mode,
    resolve_mode,
    seed_from_card,
)
from agent_org_network.runtime import AnswerMode


# ── hitl_to_mode 순수 함수 ─────────────────────────────────────────────


def test_hitl_on이면_draft_only를_반환한다():
    assert hitl_to_mode(True) == "draft_only"


def test_hitl_off이면_full을_반환한다():
    assert hitl_to_mode(False) == "full"


# ── resolve_mode — OR 결합 우선순위 ────────────────────────────────────


def test_requires_approval_True이면_draft_only():
    assert resolve_mode(requires_approval=True, hitl_on=False, current_mode="full") == "draft_only"


def test_hitl_on이면_draft_only():
    assert resolve_mode(requires_approval=False, hitl_on=True, current_mode="full") == "draft_only"


def test_둘_다_True이면_draft_only():
    assert resolve_mode(requires_approval=True, hitl_on=True, current_mode="full") == "draft_only"


def test_둘_다_False이면_full():
    assert resolve_mode(requires_approval=False, hitl_on=False, current_mode="full") == "full"


def test_backup은_draft_only로_덮지_않는다():
    """mode 우선순위: backup > draft_only — ADR 0025 결정 2."""
    result = resolve_mode(requires_approval=True, hitl_on=True, current_mode="backup")
    assert result == "backup"


def test_이미_draft_only면_그대로():
    result = resolve_mode(requires_approval=False, hitl_on=True, current_mode="draft_only")
    assert result == "draft_only"


# ── HitlToggleMap — 에이전트별 토글 상태 ──────────────────────────────


def test_토글맵_초기값이_없으면_off():
    tmap = HitlToggleMap()
    assert tmap.is_on("agent_finance") is False


def test_토글맵_set_on_후_is_on():
    tmap = HitlToggleMap()
    tmap.set("agent_finance", True)
    assert tmap.is_on("agent_finance") is True


def test_토글맵_set_off_후_is_on_false():
    tmap = HitlToggleMap()
    tmap.set("agent_finance", True)
    tmap.set("agent_finance", False)
    assert tmap.is_on("agent_finance") is False


def test_토글맵_에이전트별_독립():
    tmap = HitlToggleMap()
    tmap.set("agent_finance", True)
    tmap.set("agent_hr", False)
    assert tmap.is_on("agent_finance") is True
    assert tmap.is_on("agent_hr") is False


# ── seed_from_card — 카드 approval 정책에서 기본값 시드 ────────────────


def test_approval_when이_있는_카드는_hitl_on으로_시드한다():
    from datetime import date

    from agent_org_network.agent_card import AgentCard

    card = AgentCard(
        agent_id="agent_finance",
        owner="owner_a",
        team="finance",
        summary="재무 담당",
        domains=["finance"],
        last_reviewed_at=date(2026, 6, 27),
        approval_when=["환불"],
    )
    assert seed_from_card(card) is True


def test_approval_when이_빈_카드는_hitl_off로_시드한다():
    from datetime import date

    from agent_org_network.agent_card import AgentCard

    card = AgentCard(
        agent_id="agent_hr",
        owner="owner_b",
        team="hr",
        summary="인사 담당",
        domains=["hr"],
        last_reviewed_at=date(2026, 6, 27),
        approval_when=[],
    )
    assert seed_from_card(card) is False


def test_토글맵_seed_from_card로_초기화한다():
    from datetime import date

    from agent_org_network.agent_card import AgentCard

    card_with_approval = AgentCard(
        agent_id="agent_finance",
        owner="owner_a",
        team="finance",
        summary="재무 담당",
        domains=["finance"],
        last_reviewed_at=date(2026, 6, 27),
        approval_when=["환불"],
    )
    card_no_approval = AgentCard(
        agent_id="agent_hr",
        owner="owner_b",
        team="hr",
        summary="인사 담당",
        domains=["hr"],
        last_reviewed_at=date(2026, 6, 27),
        approval_when=[],
    )
    tmap = HitlToggleMap()
    tmap.set(card_with_approval.agent_id, seed_from_card(card_with_approval))
    tmap.set(card_no_approval.agent_id, seed_from_card(card_no_approval))

    assert tmap.is_on("agent_finance") is True
    assert tmap.is_on("agent_hr") is False


# ── under-claim 단조성 — 카드 approval은 HITL off로 못 풀림 ──────────


def test_카드_approval이_있으면_hitl_off도_draft_only_유지():
    """under-claim 단조성: requires_approval이 True면 hitl_on=False여도 draft_only."""
    result = resolve_mode(requires_approval=True, hitl_on=False, current_mode="full")
    assert result == "draft_only"


def test_카드_approval_없어도_hitl_on이면_draft_only_상향():
    """운영자 상향: requires_approval=False여도 hitl_on=True면 draft_only로 올라감."""
    result = resolve_mode(requires_approval=False, hitl_on=True, current_mode="full")
    assert result == "draft_only"


# ── 노출 불변식: mode는 Answer 절 신뢰 상태값 ─────────────────────────


def test_hitl_to_mode_반환값이_AnswerMode_멤버다():
    on_mode = hitl_to_mode(True)
    off_mode = hitl_to_mode(False)
    valid: tuple[AnswerMode, ...] = ("draft_only", "full", "backup")
    assert on_mode in valid
    assert off_mode in valid


# ── 기존 _apply_approval_gate 회귀 0 ────────────────────────────────────


def _make_card():
    from datetime import date

    from agent_org_network.agent_card import AgentCard

    return AgentCard(
        agent_id="agent-a",
        owner="owner_a",
        team="ops",
        summary="요약",
        domains=["환불"],
        last_reviewed_at=date(2026, 6, 27),
    )


def _make_ask():
    from agent_org_network.ask_org import AskOrg
    from agent_org_network.audit import InMemoryAuditLog
    from agent_org_network.classifier import FakeClassifier
    from agent_org_network.dispatch import LocalRuntimeDispatcher
    from agent_org_network.registry import Registry
    from agent_org_network.router import Router
    from agent_org_network.runtime import StubRuntime

    registry = Registry()
    registry.register(_make_card())
    router = Router(registry=registry, classifier=FakeClassifier("환불"), root_user="root")
    dispatcher = LocalRuntimeDispatcher(StubRuntime())
    return AskOrg(router=router, dispatcher=dispatcher, audit_log=InMemoryAuditLog())


def test_기존_apply_approval_gate가_requires_approval_False면_reply_그대로():
    """기존 _apply_approval_gate 회귀 테스트 — 신규 HITL 변경으로 회귀 없음."""
    from agent_org_network.ask_org import Answered
    from agent_org_network.decision import Routed

    card = _make_card()
    reply = Answered(text="답", answered_by=("agent-a", "owner_a"), mode="full")
    decision = Routed(primary=card, confidence=0.9, requires_approval=False)
    ask = _make_ask()
    result = ask._apply_approval_gate(reply, decision)  # pyright: ignore[reportPrivateUsage]
    assert result is reply


def test_기존_apply_approval_gate가_requires_approval_True이면_draft_only():
    from agent_org_network.ask_org import Answered
    from agent_org_network.decision import Routed

    card = _make_card()
    reply = Answered(text="답", answered_by=("agent-a", "owner_a"), mode="full")
    decision = Routed(primary=card, confidence=0.9, requires_approval=True)
    ask = _make_ask()
    result = ask._apply_approval_gate(reply, decision)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(result, Answered)
    assert result.mode == "draft_only"


def test_기존_apply_approval_gate가_backup은_덮지_않는다():
    from agent_org_network.ask_org import Answered
    from agent_org_network.decision import Routed

    card = _make_card()
    reply = Answered(text="답", answered_by=("agent-a", "owner_a"), mode="backup")
    decision = Routed(primary=card, confidence=0.9, requires_approval=True)
    ask = _make_ask()
    result = ask._apply_approval_gate(reply, decision)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(result, Answered)
    assert result.mode == "backup"
