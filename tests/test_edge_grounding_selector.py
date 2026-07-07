"""ADR 0038 슬라이스 B — `EdgeGroundingSelector`·`ChainGroundingSelector` [순수·결정론].

`EdgeGroundingSelector`: `Routed`에서 합의-소싱 `ComplementEdge` 이웃을 접지한다.
선택시점 재검증(생애주기 소멸 규칙·결정 5) — `intent ∈ card.domains`인 이웃만
supporting에 넣는다. `Routed` 아님/이웃 없음/유효 이웃 0 → `None`(회귀 0).

`ChainGroundingSelector`: 순서대로 시도해 첫 non-None을 돌리는 합성 selector.

이번 슬라이스는 `AskOrg`에 **배선하지 않는다** — `test_ask_org_무변경` 이 그 증거.
"""

from datetime import date

from agent_org_network.agent_card import AgentCard
from agent_org_network.complement import ComplementEdge, InMemoryEdgeStore
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.grounding import (
    ChainGroundingSelector,
    ContestedGroundingSelector,
    GroundingSet,
    first_by_agent_id,
)


def card(agent_id: str, owner: str, domains: list[str]) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary=f"{agent_id} 요약",
        domains=domains,
        last_reviewed_at=date(2026, 6, 20),
    )


CS = card("cs_ops", "owner_CS", ["보상"])
FINANCE = card("finance_ops", "owner_Finance", ["보상"])
SALES = card("sales_ops", "owner_Sales", ["영업"])  # "보상" domain 없음 — 소멸 케이스용


def _cards() -> dict[str, AgentCard]:
    return {"cs_ops": CS, "finance_ops": FINANCE, "sales_ops": SALES}


def _lookup(agent_id: str) -> AgentCard | None:
    return _cards().get(agent_id)


# ── EdgeGroundingSelector ─────────────────────────────────────────────────


class TestEdgeGroundingSelector:
    def test_Routed_엣지있으면_GroundingSet을_반환한다(self) -> None:
        from agent_org_network.grounding import EdgeGroundingSelector

        edge_store = InMemoryEdgeStore()
        edge_store.record(
            ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="finance_ops")
        )
        selector = EdgeGroundingSelector(edge_store=edge_store, card_lookup=_lookup)
        decision = Routed(primary=CS, intent="보상")

        result = selector.select(decision)

        assert result is not None
        assert result.primary.agent_id == "cs_ops"
        assert result.supporting == (FINANCE,)

    def test_Routed_엣지없으면_None이다(self) -> None:
        from agent_org_network.grounding import EdgeGroundingSelector

        selector = EdgeGroundingSelector(edge_store=InMemoryEdgeStore(), card_lookup=_lookup)
        decision = Routed(primary=CS, intent="보상")

        assert selector.select(decision) is None

    def test_이웃_카드가_없으면_등록해제_skip하고_유효이웃0이면_None(self) -> None:
        from agent_org_network.grounding import EdgeGroundingSelector

        edge_store = InMemoryEdgeStore()
        edge_store.record(
            ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="ghost_ops")
        )
        selector = EdgeGroundingSelector(edge_store=edge_store, card_lookup=_lookup)
        decision = Routed(primary=CS, intent="보상")

        assert selector.select(decision) is None

    def test_intent가_card_domains에_없으면_skip한다_생애주기_소멸(self) -> None:
        """결정 5: 선택시점 재검증 — 이웃 카드가 그 intent domain을 잃었으면 자연 소멸."""
        from agent_org_network.grounding import EdgeGroundingSelector

        edge_store = InMemoryEdgeStore()
        edge_store.record(
            ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="sales_ops")
        )
        selector = EdgeGroundingSelector(edge_store=edge_store, card_lookup=_lookup)
        decision = Routed(primary=CS, intent="보상")

        assert selector.select(decision) is None

    def test_유효_이웃과_무효_이웃이_섞이면_유효한_것만_담고_순서_결정론(self) -> None:
        from agent_org_network.grounding import EdgeGroundingSelector

        edge_store = InMemoryEdgeStore()
        edge_store.record(
            ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="sales_ops")
        )  # intent∉domains → skip
        edge_store.record(
            ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="finance_ops")
        )  # 유효
        selector = EdgeGroundingSelector(edge_store=edge_store, card_lookup=_lookup)
        decision = Routed(primary=CS, intent="보상")

        result = selector.select(decision)

        assert result is not None
        assert result.supporting == (FINANCE,)

    def test_Contested_결정이면_None이다(self) -> None:
        from agent_org_network.grounding import EdgeGroundingSelector

        edge_store = InMemoryEdgeStore()
        edge_store.record(
            ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="finance_ops")
        )
        selector = EdgeGroundingSelector(edge_store=edge_store, card_lookup=_lookup)
        decision = Contested(candidates=(CS, FINANCE), intent="보상")

        assert selector.select(decision) is None

    def test_Unowned_결정이면_None이다(self) -> None:
        from agent_org_network.grounding import EdgeGroundingSelector

        selector = EdgeGroundingSelector(edge_store=InMemoryEdgeStore(), card_lookup=_lookup)
        decision = Unowned(escalated_to="root", intent="보상")

        assert selector.select(decision) is None


# ── ChainGroundingSelector ─────────────────────────────────────────────────


class TestChainGroundingSelector:
    def test_Routed는_EdgeGroundingSelector_결과를_반환한다(self) -> None:
        from agent_org_network.grounding import EdgeGroundingSelector

        edge_store = InMemoryEdgeStore()
        edge_store.record(
            ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="finance_ops")
        )
        chain = ChainGroundingSelector(
            (
                EdgeGroundingSelector(edge_store=edge_store, card_lookup=_lookup),
                ContestedGroundingSelector(tie_break=first_by_agent_id),
            )
        )
        decision = Routed(primary=CS, intent="보상")

        result = chain.select(decision)

        assert result is not None
        assert result.primary.agent_id == "cs_ops"
        assert result.supporting == (FINANCE,)

    def test_Contested는_ContestedGroundingSelector_결과를_반환한다(self) -> None:
        from agent_org_network.grounding import EdgeGroundingSelector

        chain = ChainGroundingSelector(
            (
                EdgeGroundingSelector(edge_store=InMemoryEdgeStore(), card_lookup=_lookup),
                ContestedGroundingSelector(tie_break=first_by_agent_id),
            )
        )
        decision = Contested(candidates=(CS, FINANCE), intent="보상")

        result = chain.select(decision)

        assert result is not None
        assert result.primary.agent_id == "cs_ops"
        assert result.supporting == (FINANCE,)

    def test_둘_다_None이면_None이다(self) -> None:
        from agent_org_network.grounding import EdgeGroundingSelector

        chain = ChainGroundingSelector(
            (
                EdgeGroundingSelector(edge_store=InMemoryEdgeStore(), card_lookup=_lookup),
                ContestedGroundingSelector(tie_break=first_by_agent_id),
            )
        )
        decision = Unowned(escalated_to="root", intent="보상")

        assert chain.select(decision) is None

    def test_순서_결정론_첫_non_None이_이긴다(self) -> None:
        """두 selector가 둘 다 매칭 가능해도 튜플 순서상 앞의 것이 이긴다."""

        class _AlwaysA:
            def select(self, decision: object) -> GroundingSet | None:
                return GroundingSet(primary=CS)

        class _AlwaysB:
            def select(self, decision: object) -> GroundingSet | None:
                return GroundingSet(primary=FINANCE)

        chain_ab = ChainGroundingSelector((_AlwaysA(), _AlwaysB()))  # type: ignore[arg-type]
        chain_ba = ChainGroundingSelector((_AlwaysB(), _AlwaysA()))  # type: ignore[arg-type]
        decision = Routed(primary=CS, intent="보상")

        result_ab = chain_ab.select(decision)
        result_ba = chain_ba.select(decision)

        assert result_ab is not None and result_ab.primary.agent_id == "cs_ops"
        assert result_ba is not None and result_ba.primary.agent_id == "finance_ops"


# ── 회귀 0 — ask_org.py 무변경(슬라이스 C 아님) ───────────────────────────


def test_ask_org는_EdgeGroundingSelector를_배선하지_않는다() -> None:
    """이번 슬라이스(A+B)는 ask_org.py에 EdgeGroundingSelector를 안 꽂는다(슬라이스 C 영역).

    소스 텍스트에 식별자가 등장하지 않는지 grep으로 확인 — inert-but-tested 계약.
    """
    import inspect

    from agent_org_network import ask_org

    source = inspect.getsource(ask_org)
    assert "EdgeGroundingSelector" not in source
    assert "ChainGroundingSelector" not in source
