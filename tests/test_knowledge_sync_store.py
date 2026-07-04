"""S3(admission)↔S1 잔여(KnowledgeStore 보관) 조합 지점 (Phase 12, ADR 0033).

`accept_knowledge_sync`(knowledge_sync.py, 시그니처 무변경)가 admission *판정*까지만
하고 실 보관은 밖에 둔 것을, 이 모듈의 `accept_and_store_knowledge_sync`가 호출측에서
Admitted일 때만 `KnowledgeStore.put`으로 이어붙인다(전이≠기록: 판정은 도메인, 보관은
별 축 — 조합 함수가 그 이음매).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from agent_org_network.agent_card import AgentCard
from agent_org_network.knowledge_store import (
    InMemoryKnowledgeStore,
    accept_and_store_knowledge_sync,
)
from agent_org_network.knowledge_sync import (
    KnowledgeBundleContent,
    KnowledgeDoc,
    KnowledgeSyncSpec,
    SyncKnowledge,
)

_T0 = datetime(2026, 7, 4, 9, 0, 0, tzinfo=timezone.utc)
_REVIEWED = date(2026, 7, 4)


def _card(agent_id: str = "refund-bot", owner: str = "alice") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary=f"{agent_id} 요약",
        domains=["환불"],
        last_reviewed_at=_REVIEWED,
    )


def _spec(agent_id: str = "refund-bot", paths: tuple[str, ...] = ("docs/",)) -> KnowledgeSyncSpec:
    return KnowledgeSyncSpec(agent_id=agent_id, paths=paths)


def _content(
    agent_id: str = "refund-bot",
    documents: tuple[KnowledgeDoc, ...] = (),
    version: str = "sync-1",
    synced_at: datetime = _T0,
) -> KnowledgeBundleContent:
    return KnowledgeBundleContent(
        agent_id=agent_id, documents=documents, version=version, synced_at=synced_at
    )


def test_Admitted면_스토어에_put된다() -> None:
    store = InMemoryKnowledgeStore()
    content = _content(documents=(KnowledgeDoc(path="docs/refund.md", body="7일 이내"),))
    frame = SyncKnowledge(content=content)

    ack = accept_and_store_knowledge_sync(
        "alice", frame, _card(owner="alice"), _spec(), store
    )

    assert ack.accepted is True
    assert store.get("refund-bot") == content


def test_Rejected면_스토어에_put되지_않는다() -> None:
    store = InMemoryKnowledgeStore()
    content = _content(documents=(KnowledgeDoc(path="secrets/keys.md", body="내용"),))
    frame = SyncKnowledge(content=content)

    ack = accept_and_store_knowledge_sync(
        "alice", frame, _card(owner="alice"), _spec(), store
    )

    assert ack.accepted is False
    assert store.get("refund-bot") is None


def test_스코핑_실패면_스토어에_put되지_않는다() -> None:
    store = InMemoryKnowledgeStore()
    content = _content(documents=(KnowledgeDoc(path="docs/refund.md", body="정상"),))
    frame = SyncKnowledge(content=content)

    ack = accept_and_store_knowledge_sync(
        "mallory", frame, _card(owner="alice"), _spec(), store
    )

    assert ack.accepted is False
    assert store.get("refund-bot") is None


def test_반환_ack는_accept_knowledge_sync와_동일하다() -> None:
    """조합 함수가 보관만 덧붙이고 admission 판정 자체는 바꾸지 않는다."""
    from agent_org_network.knowledge_sync import accept_knowledge_sync

    store = InMemoryKnowledgeStore()
    content = _content(documents=(KnowledgeDoc(path="docs/refund.md", body="정상"),))
    frame = SyncKnowledge(content=content)

    plain_ack = accept_knowledge_sync("alice", frame, _card(owner="alice"), _spec())
    combined_ack = accept_and_store_knowledge_sync(
        "alice", frame, _card(owner="alice"), _spec(), store
    )

    assert plain_ack == combined_ack
