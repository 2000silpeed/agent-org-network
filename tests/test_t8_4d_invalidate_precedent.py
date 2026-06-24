"""T8.4(d) — InvalidatePrecedent 실 라우팅 제외 회귀 테스트 (ADR 0019 결정 6).

A. 미아 없음 — 무효화 후 Router 종착 불변식 (1순위 make-or-break)
B. InMemoryPrecedentStore.invalidate 단위
C. Router 안 B (lookup 순수 읽기 + p.invalidated 스킵)
D. ReevalService 와이어링 (precedents 주입·미주입·짝 정합)

전부 결정론: FakeClassifier·InMemory store·주입 clock. 실 LLM·실 외부 의존 0.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.classifier import FakeClassifier
from agent_org_network.conflict import (
    InMemoryPrecedentStore,
    Resolution,
)
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.reeval import (
    AnswerSubject,
    InMemoryReevalStore,
    InvalidatePrecedent,
    KeepPrecedent,
    PrecedentSubject,
    ReevalItem,
    ReevalService,
    SupersedePrecedent,
)
from agent_org_network.registry import Registry
from agent_org_network.router import Router

# ── 공용 픽스처 ───────────────────────────────────────────────────────────────

_TS = datetime(2026, 6, 24, 10, 0, 0, tzinfo=timezone.utc)
_TS2 = datetime(2026, 6, 24, 11, 0, 0, tzinfo=timezone.utc)
_CLOCK = lambda: _TS  # noqa: E731


def _card(agent_id: str, domains: list[str], owner: str = "owner_A") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary="요약",
        domains=domains,
        last_reviewed_at=date(2026, 6, 24),
    )


def _router(
    cards: list[AgentCard],
    intent: str,
    precedents: InMemoryPrecedentStore | None = None,
) -> Router:
    registry = Registry()
    for c in cards:
        registry.register(c)
    return Router(registry, FakeClassifier(intent), root_user="root", precedents=precedents)


def _reeval_item(
    subject: PrecedentSubject | AnswerSubject = PrecedentSubject(intent="환불"),
    owner_id: str = "owner_A",
    item_id: str = "item-001",
) -> ReevalItem:
    return ReevalItem(
        subject=subject,
        owner_id=owner_id,
        agent_id="cs_ops",
        trigger_sha="sha-001",
        flagged_at=_TS,
        item_id=item_id,
    )


# ══════════════════════════════════════════════════════════════════════════════
# A. 미아 없음 — 무효화 후 Router 종착 불변식 (1순위 make-or-break)
# ══════════════════════════════════════════════════════════════════════════════


class TestA_미아없음_무효화후_라우터_종착:
    """무효화 후 모든 라우팅 경로가 반드시 종착함을 단언한다.

    미아 = 어떤 질문도 라우팅 결과가 없는 상태. 무효화는 판례 단축경로를 끊을 뿐이고
    분류기 폴백(0→Unowned·1→Routed·≥2→Contested)이 항상 종착점이다.
    """

    def test_무효화_후_단일_카드면_Routed이고_판례적용_아님(self) -> None:
        """A-1: 무효화된 intent를 domains에 가진 카드 1개 → Routed(분류기 폴백).

        reason에 "판례 적용"이 없어야 한다(판례 경로가 아닌 분류기 폴백).
        """
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.invalidate("환불", by_owner="owner_A", at=_TS)

        router = _router([_card("cs_ops", ["환불"])], "환불", precedents=store)
        decision = router.route("환불 되나요?")

        assert isinstance(decision, Routed), f"미아 발생: {decision}"
        assert decision.primary.agent_id == "cs_ops"
        assert "판례 적용" not in decision.reason

    def test_무효화_후_카드_2개_이상이면_Contested(self) -> None:
        """A-2: 무효화된 intent를 가진 카드 ≥2개 → Contested(분류기 폴백·종착)."""
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.invalidate("환불", by_owner="owner_A", at=_TS)

        router = _router(
            [_card("cs_ops", ["환불"]), _card("sales_ops", ["환불"], owner="owner_B")],
            "환불",
            precedents=store,
        )
        decision = router.route("환불 되나요?")

        assert isinstance(decision, Contested), f"미아 발생: {decision}"
        assert len(decision.candidates) == 2

    def test_무효화_후_카드_0개면_Unowned이고_루트로_Escalation(self) -> None:
        """A-3 (핵심): 무효화된 intent, 카드 0개 → Unowned·escalated_to==root.

        이것이 미아 없음 불변식의 핵심 케이스: 어떤 질문도 미아로 남지 않는다.
        """
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.invalidate("환불", by_owner="owner_A", at=_TS)

        router = _router([], "환불", precedents=store)
        decision = router.route("환불 되나요?")

        assert isinstance(decision, Unowned), f"미아 발생(루트 escalation 없음): {decision}"
        assert decision.escalated_to == "root", (
            f"루트 escalation이 아님: escalated_to={decision.escalated_to!r}"
        )

    def test_무효화_전_판례는_기존대로_판례적용_Routed(self) -> None:
        """A-4 (대조): 무효화 전 정상 판례는 '판례 적용' reason으로 Routed."""
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))

        router = _router([_card("cs_ops", ["환불"])], "환불", precedents=store)
        decision = router.route("환불 되나요?")

        assert isinstance(decision, Routed)
        assert "판례" in decision.reason
        assert decision.primary.agent_id == "cs_ops"

    def test_무효화_전후_라우팅_결과_비교(self) -> None:
        """A-5: 무효화 전(판례 Routed) → 무효화 후(분류기 Routed) 변화 단언."""
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))

        router = _router([_card("cs_ops", ["환불"])], "환불", precedents=store)

        before = router.route("환불 되나요?")
        assert isinstance(before, Routed)
        assert "판례" in before.reason

        store.invalidate("환불", by_owner="owner_A", at=_TS)

        after = router.route("환불 되나요?")
        assert isinstance(after, Routed), f"미아 발생: {after}"
        assert "판례 적용" not in after.reason  # 분류기 폴백 경로


# ══════════════════════════════════════════════════════════════════════════════
# B. InMemoryPrecedentStore.invalidate 단위
# ══════════════════════════════════════════════════════════════════════════════


class TestB_InMemoryPrecedentStore_invalidate:
    def test_없는_intent는_None(self) -> None:
        """B-1: 판례 없는 intent → None."""
        store = InMemoryPrecedentStore(clock=_CLOCK)
        result = store.invalidate("없는_인텐트", by_owner="owner_A", at=_TS)
        assert result is None

    def test_정상_invalidate_반환값_필드(self) -> None:
        """B-2: 정상 → invalidated=True·invalidated_at==at·invalidated_by==by_owner."""
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        result = store.invalidate("환불", by_owner="owner_A", at=_TS)

        assert result is not None
        assert result.invalidated is True
        assert result.invalidated_at == _TS
        assert result.invalidated_by == "owner_A"

    def test_frozen_새_인스턴스다(self) -> None:
        """B-3: invalidate 반환값은 원본과 다른 새 frozen 인스턴스."""
        store = InMemoryPrecedentStore(clock=_CLOCK)
        original = store.record(Resolution(intent="환불", primary="cs_ops"))
        result = store.invalidate("환불", by_owner="owner_A", at=_TS)

        assert result is not None
        assert result is not original
        assert result.invalidated is True
        assert original.invalidated is False  # 원본 불변

    def test_멱등_이미_invalidated면_그대로_반환(self) -> None:
        """B-4: 이미 invalidated면 다시 표식 안 하고 그대로 반환(시각 불변)."""
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        first = store.invalidate("환불", by_owner="owner_A", at=_TS)
        second = store.invalidate("환불", by_owner="owner_A", at=_TS2)  # 다른 시각

        assert first is not None
        assert second is not None
        assert second is first  # 멱등: 같은 인스턴스 반환
        assert second.invalidated_at == _TS  # 시각 변경 없음

    def test_append_only_lookup에_그대로_남음(self) -> None:
        """B-5: invalidate 후에도 lookup(intent)에 판례가 남음(삭제 X)."""
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.invalidate("환불", by_owner="owner_A", at=_TS)

        looked_up = store.lookup("환불")
        assert looked_up is not None
        assert looked_up.invalidated is True  # 삭제 아닌 표식

    def test_append_only_list_all에_그대로_남음(self) -> None:
        """B-6: invalidate 후 list_all()에도 판례 남음(운영 면 열람 보존)."""
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.invalidate("환불", by_owner="owner_A", at=_TS)

        all_p = store.list_all()
        assert len(all_p) == 1
        assert all_p[0].invalidated is True

    def test_append_only_find_by_primary에_그대로_남음(self) -> None:
        """B-7: invalidate 후 find_by_primary()에도 판례 남음(역색인 보존)."""
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.invalidate("환불", by_owner="owner_A", at=_TS)

        by_primary = store.find_by_primary("cs_ops")
        assert len(by_primary) == 1
        assert by_primary[0].invalidated is True

    def test_append_only_history에_append됨(self) -> None:
        """B-8: invalidate 후 history에 새 인스턴스가 append됨."""
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        assert len(store.history) == 1

        store.invalidate("환불", by_owner="owner_A", at=_TS)
        assert len(store.history) == 2
        assert store.history[-1].invalidated is True

    def test_독립축_flag_stale_후_invalidate_needs_review_보존(self) -> None:
        """B-9: stale(needs_review=True)된 판례를 invalidate해도 needs_review 보존."""
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.flag_stale("환불", trigger_sha="sha-1", at=_TS)

        result = store.invalidate("환불", by_owner="owner_A", at=_TS)
        assert result is not None
        assert result.needs_review is True  # stale 보존
        assert result.invalidated is True  # 무효화 추가

    def test_독립축_invalidate_후_flag_stale_invalidated_보존(self) -> None:
        """B-10: 무효화된 판례를 flag_stale해도 invalidated 보존(역방향 독립 축)."""
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.invalidate("환불", by_owner="owner_A", at=_TS)

        store.flag_stale("환불", trigger_sha="sha-1", at=_TS)
        # invalidated가 True인 상태에서 flag_stale: needs_review가 없으면 flagged, 있으면 멱등
        # invalidated는 어떤 경우에도 보존돼야 한다
        looked_up = store.lookup("환불")
        assert looked_up is not None
        assert looked_up.invalidated is True  # 무효화 보존


# ══════════════════════════════════════════════════════════════════════════════
# C. Router 안 B — lookup 순수 읽기 + p.invalidated 스킵
# ══════════════════════════════════════════════════════════════════════════════


class TestC_Router_안B:
    def test_lookup이_invalidated_판례를_그대로_반환한다(self) -> None:
        """C-1: lookup은 순수 읽기 — invalidated 판례도 그대로 반환(store 단언 회귀 0).

        Router가 p.invalidated를 보는 것이지 store.lookup이 필터하는 게 아니다.
        """
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.invalidate("환불", by_owner="owner_A", at=_TS)

        looked_up = store.lookup("환불")
        assert looked_up is not None
        assert looked_up.invalidated is True  # lookup이 필터하지 않음

    def test_invalidated_판례면_판례경로_skip_분류기_폴백(self) -> None:
        """C-2: Router가 p.invalidated면 판례 경로를 건너뛰고 분류기 폴백.

        단일 카드이므로 분류기 폴백은 Routed(reason에 '판례 적용' 없음).
        """
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.invalidate("환불", by_owner="owner_A", at=_TS)

        router = _router([_card("cs_ops", ["환불"])], "환불", precedents=store)
        decision = router.route("환불 되나요?")

        assert isinstance(decision, Routed)
        assert "판례 적용" not in decision.reason

    def test_stale_판례는_여전히_판례경로_유지(self) -> None:
        """C-3: needs_review(stale)는 Router가 안 본다 — 판례 경로 유지(미아 없음 기존 불변식).

        stale ≠ 무효화: stale 판례도 계속 라우팅된다.
        """
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.flag_stale("환불", trigger_sha="sha-1", at=_TS)

        router = _router([_card("cs_ops", ["환불"])], "환불", precedents=store)
        decision = router.route("환불 되나요?")

        assert isinstance(decision, Routed)
        assert "판례" in decision.reason  # 판례 경로 유지


# ══════════════════════════════════════════════════════════════════════════════
# D. ReevalService 와이어링
# ══════════════════════════════════════════════════════════════════════════════


class TestD_ReevalService_와이어링:
    def test_precedents_미주입_기존_동작_하위호환(self) -> None:
        """D-1: precedents=None → review가 기존대로 동작·invalidate 호출 0(하위호환)."""
        store = InMemoryReevalStore()
        service = ReevalService(store, precedents=None)

        item = _reeval_item(
            subject=PrecedentSubject(intent="환불"),
            owner_id="owner_A",
            item_id="item-d1",
        )
        store.add(item)

        reviewed = service.review("item-d1", InvalidatePrecedent(by_owner="owner_A"))

        assert reviewed.status == "reviewed"
        # precedents가 None이므로 invalidate 효과 없음 — 검증은 precedents store 없음으로 충분

    def test_PrecedentSubject_x_InvalidatePrecedent_짝_판례_무효화됨(self) -> None:
        """D-2: (PrecedentSubject(intent), InvalidatePrecedent) 짝 → 판례가 invalidated=True."""
        precedent_store = InMemoryPrecedentStore(clock=_CLOCK)
        precedent_store.record(Resolution(intent="환불", primary="cs_ops"))

        reeval_store = InMemoryReevalStore()
        service = ReevalService(reeval_store, precedents=precedent_store, clock=_CLOCK)

        item = _reeval_item(
            subject=PrecedentSubject(intent="환불"),
            owner_id="owner_A",
            item_id="item-d2",
        )
        reeval_store.add(item)

        service.review("item-d2", InvalidatePrecedent(by_owner="owner_A"))

        p = precedent_store.lookup("환불")
        assert p is not None
        assert p.invalidated is True

    def test_D2_이어서_Router_분류기_폴백_미아없음(self) -> None:
        """D-2 연속: 무효화 후 같은 intent Router 라우팅이 분류기 폴백으로 종착."""
        precedent_store = InMemoryPrecedentStore(clock=_CLOCK)
        precedent_store.record(Resolution(intent="환불", primary="cs_ops"))

        reeval_store = InMemoryReevalStore()
        service = ReevalService(reeval_store, precedents=precedent_store, clock=_CLOCK)

        item = _reeval_item(
            subject=PrecedentSubject(intent="환불"),
            owner_id="owner_A",
            item_id="item-d2b",
        )
        reeval_store.add(item)
        service.review("item-d2b", InvalidatePrecedent(by_owner="owner_A"))

        # 카드 0개 → Unowned·루트 escalation(미아 없음 핵심)
        router = _router([], "환불", precedents=precedent_store)
        decision = router.route("환불 되나요?")
        assert isinstance(decision, Unowned), f"미아 발생: {decision}"
        assert decision.escalated_to == "root"

    def test_clock_주입으로_invalidated_at_결정론_단언(self) -> None:
        """D-3: clock 주입 → invalidated_at이 주입 clock 값과 일치."""
        precedent_store = InMemoryPrecedentStore(clock=_CLOCK)
        precedent_store.record(Resolution(intent="환불", primary="cs_ops"))

        reeval_store = InMemoryReevalStore()
        service = ReevalService(reeval_store, precedents=precedent_store, clock=_CLOCK)

        item = _reeval_item(
            subject=PrecedentSubject(intent="환불"),
            owner_id="owner_A",
            item_id="item-d3",
        )
        reeval_store.add(item)
        service.review("item-d3", InvalidatePrecedent(by_owner="owner_A"))

        p = precedent_store.lookup("환불")
        assert p is not None
        assert p.invalidated_at == _TS  # clock 주입 결정론

    def test_축_어긋난_짝_AnswerSubject_x_InvalidatePrecedent_무시(self) -> None:
        """D-4: (AnswerSubject, InvalidatePrecedent) 짝 → invalidate 호출 0·전이만."""
        precedent_store = InMemoryPrecedentStore(clock=_CLOCK)
        precedent_store.record(Resolution(intent="환불", primary="cs_ops"))

        reeval_store = InMemoryReevalStore()
        service = ReevalService(reeval_store, precedents=precedent_store, clock=_CLOCK)

        item = _reeval_item(
            subject=AnswerSubject(audit_index=0),  # Answer 축
            owner_id="owner_A",
            item_id="item-d4",
        )
        reeval_store.add(item)

        # AnswerSubject + InvalidatePrecedent = 축 어긋남 → 에러 없이 전이만
        reviewed = service.review("item-d4", InvalidatePrecedent(by_owner="owner_A"))

        assert reviewed.status == "reviewed"  # 전이는 그대로

        # 판례는 무효화되지 않음(부작용 0)
        p = precedent_store.lookup("환불")
        assert p is not None
        assert p.invalidated is False

    def test_PrecedentSubject_x_KeepPrecedent_invalidate_부작용_없음(self) -> None:
        """D-5: (PrecedentSubject, KeepPrecedent) → invalidate 부작용 0·전이만."""
        precedent_store = InMemoryPrecedentStore(clock=_CLOCK)
        precedent_store.record(Resolution(intent="환불", primary="cs_ops"))

        reeval_store = InMemoryReevalStore()
        service = ReevalService(reeval_store, precedents=precedent_store, clock=_CLOCK)

        item = _reeval_item(
            subject=PrecedentSubject(intent="환불"),
            owner_id="owner_A",
            item_id="item-d5",
        )
        reeval_store.add(item)

        reviewed = service.review("item-d5", KeepPrecedent(by_owner="owner_A"))

        assert reviewed.status == "reviewed"

        p = precedent_store.lookup("환불")
        assert p is not None
        assert p.invalidated is False  # KeepPrecedent는 무효화 안 함

    def test_PrecedentSubject_x_SupersedePrecedent_invalidate_부작용_없음(self) -> None:
        """D-6: (PrecedentSubject, SupersedePrecedent) → invalidate 부작용 0(d 범위 밖)."""
        precedent_store = InMemoryPrecedentStore(clock=_CLOCK)
        precedent_store.record(Resolution(intent="환불", primary="cs_ops"))

        reeval_store = InMemoryReevalStore()
        service = ReevalService(reeval_store, precedents=precedent_store, clock=_CLOCK)

        item = _reeval_item(
            subject=PrecedentSubject(intent="환불"),
            owner_id="owner_A",
            item_id="item-d6",
        )
        reeval_store.add(item)

        reviewed = service.review(
            "item-d6", SupersedePrecedent(by_owner="owner_A", new_primary="cs_ops_new")
        )

        assert reviewed.status == "reviewed"

        p = precedent_store.lookup("환불")
        assert p is not None
        assert p.invalidated is False  # SupersedePrecedent는 무효화 안 함

    def test_1인칭_위반_기존_동작_보존(self) -> None:
        """D-7: by_owner≠owner_id → ValueError(InvalidatePrecedent 와이어링이 깨지 않음)."""
        precedent_store = InMemoryPrecedentStore(clock=_CLOCK)
        precedent_store.record(Resolution(intent="환불", primary="cs_ops"))

        reeval_store = InMemoryReevalStore()
        service = ReevalService(reeval_store, precedents=precedent_store, clock=_CLOCK)

        item = _reeval_item(
            subject=PrecedentSubject(intent="환불"),
            owner_id="owner_A",
            item_id="item-d7",
        )
        reeval_store.add(item)

        with pytest.raises(ValueError):
            service.review("item-d7", InvalidatePrecedent(by_owner="타인"))

        # 1인칭 위반으로 invalidate 미호출 → 판례 무효화 없음
        p = precedent_store.lookup("환불")
        assert p is not None
        assert p.invalidated is False

    def test_미존재_item_기존_동작_보존(self) -> None:
        """D-8: 미존재 item_id → ValueError(와이어링이 안 깸)."""
        reeval_store = InMemoryReevalStore()
        service = ReevalService(reeval_store, precedents=None, clock=_CLOCK)

        with pytest.raises(ValueError, match="미존재"):
            service.review("nonexistent", InvalidatePrecedent(by_owner="owner_A"))

    def test_이미_reviewed_멱등_기존_동작_보존(self) -> None:
        """D-9: 이미 reviewed면 멱등(두 번째 review 호출에서도 invalidate 재호출 X)."""
        precedent_store = InMemoryPrecedentStore(clock=_CLOCK)
        precedent_store.record(Resolution(intent="환불", primary="cs_ops"))

        reeval_store = InMemoryReevalStore()
        service = ReevalService(reeval_store, precedents=precedent_store, clock=_CLOCK)

        item = _reeval_item(
            subject=PrecedentSubject(intent="환불"),
            owner_id="owner_A",
            item_id="item-d9",
        )
        reeval_store.add(item)

        first = service.review("item-d9", InvalidatePrecedent(by_owner="owner_A"))
        second = service.review("item-d9", InvalidatePrecedent(by_owner="owner_A"))

        assert first.status == "reviewed"
        assert second.status == "reviewed"

        # 판례는 무효화됨(첫 번째 호출)
        p = precedent_store.lookup("환불")
        assert p is not None
        assert p.invalidated is True
