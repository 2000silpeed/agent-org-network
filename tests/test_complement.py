"""ADR 0038 슬라이스 A — `ComplementEdge`·`EdgeStore`(포트)·`InMemoryEdgeStore` [순수·결정론].

`Precedent`(라우팅 학습) : `ComplementEdge`(접지 학습) 대칭(결정 1). 유향(primary→
supporting)·자기 접지 거부·빈 intent 거부(model_validator)·`InMemoryEdgeStore`
멱등(중복 supporting 무시)·순서 결정론(삽입 순)을 잠근다.
"""

import pytest
from pydantic import ValidationError

from agent_org_network.complement import ComplementEdge, InMemoryEdgeStore


# ── ComplementEdge 값 객체 ────────────────────────────────────────────────


def test_ComplementEdge는_intent_primary_id_supporting_id를_가진다() -> None:
    edge = ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="finance_ops")
    assert edge.intent == "보상"
    assert edge.primary_id == "cs_ops"
    assert edge.supporting_id == "finance_ops"


def test_ComplementEdge는_frozen이다() -> None:
    edge = ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="finance_ops")
    with pytest.raises(ValidationError):
        edge.primary_id = "sales_ops"  # type: ignore[misc]


def test_ComplementEdge_자기_접지는_거부된다() -> None:
    with pytest.raises(ValidationError):
        ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="cs_ops")


def test_ComplementEdge_빈_intent는_거부된다() -> None:
    with pytest.raises(ValidationError):
        ComplementEdge(intent="", primary_id="cs_ops", supporting_id="finance_ops")


def test_ComplementEdge_빈_primary_id는_거부된다() -> None:
    with pytest.raises(ValidationError):
        ComplementEdge(intent="보상", primary_id="", supporting_id="finance_ops")


def test_ComplementEdge_빈_supporting_id는_거부된다() -> None:
    with pytest.raises(ValidationError):
        ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="")


def test_ComplementEdge는_유향이다() -> None:
    """primary_id/supporting_id 순서가 바뀌면 다른 엣지(값 동등성으로 확인)."""
    forward = ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="finance_ops")
    backward = ComplementEdge(intent="보상", primary_id="finance_ops", supporting_id="cs_ops")
    assert forward != backward


# ── InMemoryEdgeStore ────────────────────────────────────────────────────


def test_record_후_neighbors가_supporting_agent_id를_반환한다() -> None:
    store = InMemoryEdgeStore()
    store.record(ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="finance_ops"))
    assert store.neighbors("보상", "cs_ops") == ("finance_ops",)


def test_없는_intent_primary는_빈_튜플() -> None:
    store = InMemoryEdgeStore()
    assert store.neighbors("보상", "cs_ops") == ()


def test_record는_멱등이다_중복_supporting_무시() -> None:
    store = InMemoryEdgeStore()
    edge = ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="finance_ops")
    store.record(edge)
    store.record(edge)
    store.record(edge)
    assert store.neighbors("보상", "cs_ops") == ("finance_ops",)


def test_복수_supporting은_삽입_순서_결정론이다() -> None:
    store = InMemoryEdgeStore()
    store.record(ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="finance_ops"))
    store.record(ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="sales_ops"))
    store.record(ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="legal_ops"))
    assert store.neighbors("보상", "cs_ops") == ("finance_ops", "sales_ops", "legal_ops")


def test_다른_intent는_독립_색인이다() -> None:
    store = InMemoryEdgeStore()
    store.record(ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="finance_ops"))
    store.record(ComplementEdge(intent="환불", primary_id="cs_ops", supporting_id="sales_ops"))
    assert store.neighbors("보상", "cs_ops") == ("finance_ops",)
    assert store.neighbors("환불", "cs_ops") == ("sales_ops",)


def test_다른_primary는_독립_색인이다() -> None:
    store = InMemoryEdgeStore()
    store.record(ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="finance_ops"))
    store.record(ComplementEdge(intent="보상", primary_id="sales_ops", supporting_id="legal_ops"))
    assert store.neighbors("보상", "cs_ops") == ("finance_ops",)
    assert store.neighbors("보상", "sales_ops") == ("legal_ops",)
