"""T9.3(b) — HITL 토글이 AskOrg._apply_approval_gate에 실제로 반영되는지(ADR 0025).

기존 T9.3(a)는 hitl.py 순수 로직만(소비처 0). 여기선 `AskOrg(hitl_toggles=...)` 주입 시
`_apply_approval_gate`가 `resolve_mode`(카드 approval_when + 토글 OR 결합)로 mode를
결정하는지 검증한다. 미주입이면 기존 동작(카드만 봄) 100% 보존 — test_approval_collaboration.py·
test_hitl_toggle.py 회귀 테스트가 그 하위호환을 이미 지킨다.

결정론: FakeClassifier + StubRuntime + InMemoryAuditLog. 실 LLM 0.
"""

from datetime import date, datetime, timezone

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import AskOrg, Answered
from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.classifier import FakeClassifier
from agent_org_network.decision import Routed
from agent_org_network.dispatch import LocalRuntimeDispatcher
from agent_org_network.hitl import HitlToggleMap
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import StubRuntime
from agent_org_network.user import User


def _fixed_clock() -> datetime:
    return datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


def _card(agent_id: str, domains: list[str], approval_when: list[str] | None = None) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner="owner_a",
        team="ops",
        summary="요약",
        domains=domains,
        last_reviewed_at=date(2026, 6, 20),
        approval_when=approval_when or [],
    )


def _ask_org_with(card: AgentCard, intent: str, hitl_toggles: HitlToggleMap | None) -> AskOrg:
    registry = Registry()
    registry.register(card)
    classifier = FakeClassifier(intent)
    router = Router(registry, classifier, root_user="root")
    return AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=InMemoryAuditLog(),
        clock=_fixed_clock,
        hitl_toggles=hitl_toggles,
    )


# ── _apply_approval_gate + hitl_toggles 직접 단위 테스트 ──────────────────


def test_hitl_toggles_주입_토글_on이면_카드_approval_없어도_draft_only():
    card = _card("agent_a", ["환불"])  # approval_when 없음
    toggles = HitlToggleMap()
    toggles.set("agent_a", True)
    ask = _ask_org_with(card, "환불", toggles)

    routed = Routed(primary=card, requires_approval=False)
    reply = Answered(text="답", answered_by=("owner_a", "agent_a"), mode="full")
    result = ask._apply_approval_gate(reply, routed)  # pyright: ignore[reportPrivateUsage]

    assert isinstance(result, Answered)
    assert result.mode == "draft_only"


def test_hitl_toggles_주입_토글_off이고_카드_approval도_없으면_full():
    card = _card("agent_a", ["환불"])
    toggles = HitlToggleMap()
    toggles.set("agent_a", False)
    ask = _ask_org_with(card, "환불", toggles)

    routed = Routed(primary=card, requires_approval=False)
    reply = Answered(text="답", answered_by=("owner_a", "agent_a"), mode="full")
    result = ask._apply_approval_gate(reply, routed)  # pyright: ignore[reportPrivateUsage]

    assert result is reply
    assert isinstance(result, Answered)
    assert result.mode == "full"


def test_hitl_toggles_주입_카드_approval_True면_토글_off여도_draft_only():
    """under-claim 단조성 — 카드 approval_when이 토글 off로 안 풀린다."""
    card = _card("agent_a", ["환불"], approval_when=["환불"])
    toggles = HitlToggleMap()
    toggles.set("agent_a", False)
    ask = _ask_org_with(card, "환불", toggles)

    routed = Routed(primary=card, requires_approval=True)
    reply = Answered(text="답", answered_by=("owner_a", "agent_a"), mode="full")
    result = ask._apply_approval_gate(reply, routed)  # pyright: ignore[reportPrivateUsage]

    assert isinstance(result, Answered)
    assert result.mode == "draft_only"


def test_hitl_toggles_주입_backup은_토글_on이어도_보존():
    card = _card("agent_a", ["환불"])
    toggles = HitlToggleMap()
    toggles.set("agent_a", True)
    ask = _ask_org_with(card, "환불", toggles)

    routed = Routed(primary=card, requires_approval=False)
    reply = Answered(text="백업 답", answered_by=("owner_a", "agent_a"), mode="backup")
    result = ask._apply_approval_gate(reply, routed)  # pyright: ignore[reportPrivateUsage]

    assert isinstance(result, Answered)
    assert result.mode == "backup"


def test_hitl_toggles_미주입이면_기존_동작_보존():
    """hitl_toggles 미주입 — 카드 approval_when만 본다(하위호환, 회귀 0)."""
    card = _card("agent_a", ["환불"])  # approval_when 없음
    ask = _ask_org_with(card, "환불", None)

    routed = Routed(primary=card, requires_approval=False)
    reply = Answered(text="답", answered_by=("owner_a", "agent_a"), mode="full")
    result = ask._apply_approval_gate(reply, routed)  # pyright: ignore[reportPrivateUsage]

    assert result is reply
    assert isinstance(result, Answered)
    assert result.mode == "full"


# ── handle() 경유 end-to-end — 토글이 다음 답의 mode에 반영 ───────────────


def test_handle_경유_토글_on이면_Answered_mode_draft_only():
    card = _card("agent_a", ["환불"])
    toggles = HitlToggleMap()
    toggles.set("agent_a", True)
    ask = _ask_org_with(card, "환불", toggles)
    user = User(id="u1")

    reply = ask.handle("환불 문의드려요", user)

    assert isinstance(reply, Answered)
    assert reply.mode == "draft_only"


def test_handle_경유_토글_off이면_Answered_mode_full():
    card = _card("agent_a", ["환불"])
    toggles = HitlToggleMap()
    toggles.set("agent_a", False)
    ask = _ask_org_with(card, "환불", toggles)
    user = User(id="u1")

    reply = ask.handle("환불 문의드려요", user)

    assert isinstance(reply, Answered)
    assert reply.mode == "full"


def test_handle_경유_토글_변경이_다음_답에_결정론으로_반영():
    """콘솔 토글 on→off 순차 변경이 각각의 다음 답 mode에 결정론으로 반영된다."""
    card = _card("agent_a", ["환불"])
    toggles = HitlToggleMap()
    ask = _ask_org_with(card, "환불", toggles)
    user = User(id="u1")

    toggles.set("agent_a", True)
    reply1 = ask.handle("환불 문의1", user)
    assert isinstance(reply1, Answered)
    assert reply1.mode == "draft_only"

    toggles.set("agent_a", False)
    reply2 = ask.handle("환불 문의2", user)
    assert isinstance(reply2, Answered)
    assert reply2.mode == "full"
