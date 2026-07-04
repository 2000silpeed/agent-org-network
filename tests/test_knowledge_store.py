"""Knowledge Store 포트 — 본문 보관소 (Phase 12 S1 잔여, ADR 0033 S1 shape 절).

범위: 결정론(주입 clock)만 — 순수 보관(전이 아님)·더 새 version만 수용·agent_id
미등록 거부(등록 무결성)·stale 판정(주입 now·threshold_s).

  - put/get 왕복
  - 더 새 version만 수용(구 version 덮어쓰기 거부)
  - 미등록 agent_id get은 None
  - is_stale — threshold 미만/초과·정확히 경계
  - AON_KNOWLEDGE_STALE_SECONDS 설정값 파서(기본 1800초)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_org_network.knowledge_store import (
    DEFAULT_KNOWLEDGE_STALE_SECONDS,
    InMemoryKnowledgeStore,
    knowledge_stale_seconds,
)
from agent_org_network.knowledge_sync import KnowledgeBundleContent, KnowledgeDoc

_T0 = datetime(2026, 7, 4, 9, 0, 0, tzinfo=timezone.utc)


def _content(
    agent_id: str = "refund-bot",
    documents: tuple[KnowledgeDoc, ...] = (),
    version: str = "sync-1",
    synced_at: datetime = _T0,
) -> KnowledgeBundleContent:
    return KnowledgeBundleContent(
        agent_id=agent_id, documents=documents, version=version, synced_at=synced_at
    )


# ── put/get 왕복 ──────────────────────────────────────────────────────────


def test_put한_본문을_같은_agent_id로_get할_수_있다() -> None:
    store = InMemoryKnowledgeStore()
    content = _content(
        documents=(KnowledgeDoc(path="docs/policy.md", body="환불 정책"),)
    )
    store.put(content)

    result = store.get("refund-bot")

    assert result == content


def test_미등록_agent_id는_get시_None이다() -> None:
    store = InMemoryKnowledgeStore()

    assert store.get("존재하지않는-agent") is None


# ── 더 새 version만 수용 ──────────────────────────────────────────────────


def test_더_새_version이면_교체된다() -> None:
    store = InMemoryKnowledgeStore()
    store.put(_content(version="sync-1", synced_at=_T0))
    newer = _content(
        version="sync-2",
        synced_at=_T0 + timedelta(minutes=5),
        documents=(KnowledgeDoc(path="docs/policy.md", body="갱신된 정책"),),
    )

    store.put(newer)

    assert store.get("refund-bot") == newer


def test_더_구_version은_거부되고_기존_것이_유지된다() -> None:
    store = InMemoryKnowledgeStore()
    newer = _content(version="sync-2", synced_at=_T0 + timedelta(minutes=5))
    store.put(newer)

    older = _content(version="sync-1", synced_at=_T0)
    store.put(older)

    assert store.get("refund-bot") == newer


def test_같은_version_재put은_멱등이다() -> None:
    store = InMemoryKnowledgeStore()
    content = _content(version="sync-1")
    store.put(content)
    store.put(content)

    assert store.get("refund-bot") == content


# ── is_stale ──────────────────────────────────────────────────────────────


def test_threshold_미만이면_stale이_아니다() -> None:
    store = InMemoryKnowledgeStore()
    store.put(_content(synced_at=_T0))

    now = _T0 + timedelta(seconds=100)

    assert store.is_stale("refund-bot", now=now, threshold_s=1800) is False


def test_threshold_초과면_stale이다() -> None:
    store = InMemoryKnowledgeStore()
    store.put(_content(synced_at=_T0))

    now = _T0 + timedelta(seconds=1801)

    assert store.is_stale("refund-bot", now=now, threshold_s=1800) is True


def test_threshold_경계값은_stale이_아니다() -> None:
    store = InMemoryKnowledgeStore()
    store.put(_content(synced_at=_T0))

    now = _T0 + timedelta(seconds=1800)

    assert store.is_stale("refund-bot", now=now, threshold_s=1800) is False


def test_미등록_agent_id는_stale_판정시_True다() -> None:
    """본문이 아예 없으면 신선도를 논할 대상이 없다 — 안전측 기본값(낡음 취급)."""
    store = InMemoryKnowledgeStore()

    assert store.is_stale("존재하지않는-agent", now=_T0, threshold_s=1800) is True


# ── AON_KNOWLEDGE_STALE_SECONDS 설정값 파서 ────────────────────────────────


def test_미설정이면_기본값_1800초다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AON_KNOWLEDGE_STALE_SECONDS", raising=False)

    assert knowledge_stale_seconds() == DEFAULT_KNOWLEDGE_STALE_SECONDS
    assert DEFAULT_KNOWLEDGE_STALE_SECONDS == 1800


def test_설정되면_그_값을_쓴다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_KNOWLEDGE_STALE_SECONDS", "60")

    assert knowledge_stale_seconds() == 60


def test_빈문자열_설정은_기본값으로_폴백한다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_KNOWLEDGE_STALE_SECONDS", "")

    assert knowledge_stale_seconds() == DEFAULT_KNOWLEDGE_STALE_SECONDS
