"""Phase 12 SQLite durable 확장 — SqliteAnswerRecordStore·SqliteCorrectionStore·
SqliteKnowledgeStore·카드 등록 저널 리플레이 통합 테스트.

`test_sqlite_stores.py`(T9.8 세션/토큰)와 같은 정신 — `tmp_path` DB 파일로 재시작
생존을 검증한다. 결정론: 주입 clock, 실 LLM/네트워크 0.

검증 축:
  - `AnswerRecordStore`·`CorrectionStore` — append-only 계약(UPDATE 없음·원 레코드
    불변), 재오픈 후 보존.
  - `KnowledgeStore` — put 최신 교체(upsert)·get·is_stale, 재오픈 후 보존.
  - 카드 라이브 등록·오너 변경의 저널 리플레이 — 재기동 시 YAML 시드 → 저널
    리플레이로 라이브 Registry 복원(오너 변경 반영·구 owner 아님), admission 경유
    (무효 카드 복원 금지).
  - `AON_DB` off면 기존 InMemory 그대로(회귀 0).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from agent_org_network.agent_card import AgentCard
from agent_org_network.answer_record import (
    AnswerFeedback,
    AnswerRecord,
    AnswerRecordStore,
    CorrectionEvent,
    CorrectionStore,
    FeedbackStore,
)
from agent_org_network.knowledge_store import KnowledgeStore
from agent_org_network.knowledge_sync import KnowledgeBundleContent, KnowledgeDoc
from agent_org_network.registry import Registry
from agent_org_network.user import User

T0 = datetime(2026, 7, 5, 0, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=10)


# ── SqliteAnswerRecordStore ──────────────────────────────────────────────────


def _record(
    record_id: str = "rec-1",
    agent_id: str = "cs_ops",
    answered_by: str = "alice",
    at: datetime = T0,
    needs_correction_review: bool = False,
) -> AnswerRecord:
    return AnswerRecord(
        record_id=record_id,
        question="환불 어떻게 하나요?",
        answer_text="영업일 3일 내 환불됩니다.",
        answered_by=answered_by,
        agent_id=agent_id,
        mode="full",
        session_id="sess-1",
        answered_at=at,
        needs_correction_review=needs_correction_review,
    )


def test_SqliteAnswerRecordStore_는_AnswerRecordStore_프로토콜을_만족한다(
    tmp_path: Path,
) -> None:
    from agent_org_network.sqlite_stores import SqliteAnswerRecordStore

    store: AnswerRecordStore = SqliteAnswerRecordStore(tmp_path / "a.db")
    assert callable(store.add)
    assert callable(store.get)
    assert callable(store.for_agent)


def test_answer_record_add_get_재오픈_후_보존(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteAnswerRecordStore

    db = tmp_path / "a.db"
    store = SqliteAnswerRecordStore(db)
    rec = _record()
    store.add(rec)
    store.close()

    reopened = SqliteAnswerRecordStore(db)
    got = reopened.get("rec-1")
    assert got == rec


def test_answer_record_for_agent_는_해당_agent_레코드만(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteAnswerRecordStore

    store = SqliteAnswerRecordStore(tmp_path / "a.db")
    store.add(_record(record_id="rec-1", agent_id="cs_ops"))
    store.add(_record(record_id="rec-2", agent_id="finance_ops"))
    store.add(_record(record_id="rec-3", agent_id="cs_ops"))

    cs_records = store.for_agent("cs_ops")
    assert {r.record_id for r in cs_records} == {"rec-1", "rec-3"}


def test_answer_record_미존재_get_None(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteAnswerRecordStore

    store = SqliteAnswerRecordStore(tmp_path / "a.db")
    assert store.get("ghost") is None


def test_answer_record_needs_correction_review_왕복(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteAnswerRecordStore

    db = tmp_path / "a.db"
    store = SqliteAnswerRecordStore(db)
    rec = _record(needs_correction_review=True)
    store.add(rec)
    store.close()

    reopened = SqliteAnswerRecordStore(db)
    got = reopened.get("rec-1")
    assert got is not None
    assert got.needs_correction_review is True


# ── SqliteCorrectionStore ────────────────────────────────────────────────────


def _correction(
    event_id: str, record_id: str = "rec-1", by_owner: str = "alice", at: datetime = T0
) -> CorrectionEvent:
    return CorrectionEvent(
        event_id=event_id,
        record_id=record_id,
        corrected_text="정정된 답변입니다.",
        by_owner=by_owner,
        rationale="사실 오류 수정",
        corrected_at=at,
    )


def test_SqliteCorrectionStore_는_CorrectionStore_프로토콜을_만족한다(
    tmp_path: Path,
) -> None:
    from agent_org_network.sqlite_stores import SqliteCorrectionStore

    store: CorrectionStore = SqliteCorrectionStore(tmp_path / "c.db")
    assert callable(store.append)
    assert callable(store.for_record)


def test_correction_append_순서_보존_재오픈_후에도(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteCorrectionStore

    db = tmp_path / "c.db"
    store = SqliteCorrectionStore(db)
    e1 = _correction("e1", at=T0)
    e2 = _correction("e2", at=T1)
    store.append(e1)
    store.append(e2)
    store.close()

    reopened = SqliteCorrectionStore(db)
    events = reopened.for_record("rec-1")
    assert [e.event_id for e in events] == ["e1", "e2"]


def test_correction_append_only_원레코드_불변_UPDATE_없음(tmp_path: Path) -> None:
    """append-only 계약 — 같은 record_id 재정정도 새 이벤트로 쌓이고 기존 이벤트는 불변."""
    from agent_org_network.sqlite_stores import SqliteCorrectionStore

    store = SqliteCorrectionStore(tmp_path / "c.db")
    e1 = _correction("e1", at=T0)
    store.append(e1)
    e2 = _correction("e2", at=T1)
    store.append(e2)

    events = store.for_record("rec-1")
    assert len(events) == 2
    # 기존 이벤트 필드가 그대로(수정되지 않음).
    assert events[0].event_id == "e1"
    assert events[0].corrected_at == T0


def test_correction_for_record_은_다른_record는_안_섞는다(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteCorrectionStore

    store = SqliteCorrectionStore(tmp_path / "c.db")
    store.append(_correction("e1", record_id="rec-1"))
    store.append(_correction("e2", record_id="rec-2"))

    assert [e.event_id for e in store.for_record("rec-1")] == ["e1"]
    assert [e.event_id for e in store.for_record("rec-2")] == ["e2"]


# ── SqliteFeedbackStore ──────────────────────────────────────────────────────


def _feedback(
    record_id: str = "rec-1",
    verdict: str = "bad",
    submitted_by: str = "mcp_guest",
    comment: str = "",
    at: datetime = T0,
) -> AnswerFeedback:
    return AnswerFeedback(
        record_id=record_id,
        verdict=verdict,  # type: ignore[arg-type]
        comment=comment,
        submitted_by=submitted_by,
        submitted_at=at,
    )


def test_SqliteFeedbackStore_는_FeedbackStore_프로토콜을_만족한다(
    tmp_path: Path,
) -> None:
    from agent_org_network.sqlite_stores import SqliteFeedbackStore

    store: FeedbackStore = SqliteFeedbackStore(tmp_path / "f.db")
    assert callable(store.upsert)
    assert callable(store.latest_for_record)
    assert callable(store.for_record)


def test_feedback_upsert_latest_재오픈_후_보존(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteFeedbackStore

    db = tmp_path / "f.db"
    store = SqliteFeedbackStore(db)
    fb = _feedback(verdict="good", comment="정확해요")
    store.upsert(fb)
    store.close()

    reopened = SqliteFeedbackStore(db)
    got = reopened.latest_for_record("rec-1")
    assert got == fb


def test_feedback_upsert_마음_바꿈_최신_판정_교체(tmp_path: Path) -> None:
    """같은 (record_id, submitted_by) 재제출은 최신 판정으로 덮는다(계획 §10.2)."""
    from agent_org_network.sqlite_stores import SqliteFeedbackStore

    store = SqliteFeedbackStore(tmp_path / "f.db")
    store.upsert(_feedback(verdict="bad", at=T0))
    store.upsert(_feedback(verdict="good", comment="다시 보니 맞네요", at=T1))

    latest = store.latest_for_record("rec-1")
    assert latest is not None
    assert latest.verdict == "good"
    assert latest.comment == "다시 보니 맞네요"


def test_feedback_이력은_전량_보존_재오픈_후에도(tmp_path: Path) -> None:
    """upsert가 판정을 덮어도 for_record는 append 이력 전량을 돌려준다(전이 ≠ 기록)."""
    from agent_org_network.sqlite_stores import SqliteFeedbackStore

    db = tmp_path / "f.db"
    store = SqliteFeedbackStore(db)
    store.upsert(_feedback(verdict="bad", at=T0))
    store.upsert(_feedback(verdict="good", at=T1))
    store.close()

    reopened = SqliteFeedbackStore(db)
    history = reopened.for_record("rec-1")
    assert [fb.verdict for fb in history] == ["bad", "good"]


def test_feedback_서로_다른_질문자는_각각_별행(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteFeedbackStore

    store = SqliteFeedbackStore(tmp_path / "f.db")
    store.upsert(_feedback(submitted_by="mcp_guest", verdict="bad", at=T0))
    store.upsert(_feedback(submitted_by="alice", verdict="good", at=T1))

    history = store.for_record("rec-1")
    assert len(history) == 2
    # latest_for_record는 submitted_at 최대(alice의 good).
    latest = store.latest_for_record("rec-1")
    assert latest is not None
    assert latest.submitted_by == "alice"


def test_feedback_for_record_은_다른_record는_안_섞는다(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteFeedbackStore

    store = SqliteFeedbackStore(tmp_path / "f.db")
    store.upsert(_feedback(record_id="rec-1", verdict="bad"))
    store.upsert(_feedback(record_id="rec-2", verdict="good"))

    assert [fb.verdict for fb in store.for_record("rec-1")] == ["bad"]
    assert [fb.verdict for fb in store.for_record("rec-2")] == ["good"]


def test_feedback_미등록_record_latest_None(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteFeedbackStore

    store = SqliteFeedbackStore(tmp_path / "f.db")
    assert store.latest_for_record("ghost") is None
    assert store.for_record("ghost") == []


# ── SqliteKnowledgeStore ─────────────────────────────────────────────────────


def _bundle(
    agent_id: str = "cs_ops", version: str = "v1", synced_at: datetime = T0
) -> KnowledgeBundleContent:
    return KnowledgeBundleContent(
        agent_id=agent_id,
        documents=(KnowledgeDoc(path="refund.md", body="환불 정책 본문"),),
        version=version,
        synced_at=synced_at,
    )


def test_SqliteKnowledgeStore_는_KnowledgeStore_프로토콜을_만족한다(
    tmp_path: Path,
) -> None:
    from agent_org_network.sqlite_stores import SqliteKnowledgeStore

    store: KnowledgeStore = SqliteKnowledgeStore(tmp_path / "k.db")
    assert callable(store.put)
    assert callable(store.get)
    assert callable(store.is_stale)


def test_knowledge_put_get_재오픈_후_보존(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteKnowledgeStore

    db = tmp_path / "k.db"
    store = SqliteKnowledgeStore(db)
    content = _bundle()
    store.put(content)
    store.close()

    reopened = SqliteKnowledgeStore(db)
    got = reopened.get("cs_ops")
    assert got == content


def test_knowledge_put_은_최신_version만_수용_upsert(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteKnowledgeStore

    store = SqliteKnowledgeStore(tmp_path / "k.db")
    older = _bundle(version="v1", synced_at=T0)
    newer = _bundle(version="v2", synced_at=T1)
    store.put(newer)
    store.put(older)  # 더 오래된 버전 — 무시.

    got = store.get("cs_ops")
    assert got is not None
    assert got.version == "v2"


def test_knowledge_put_같은_version_재put_은_무시(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteKnowledgeStore

    store = SqliteKnowledgeStore(tmp_path / "k.db")
    first = _bundle(version="v1", synced_at=T0)
    store.put(first)
    same_version_but_different = KnowledgeBundleContent(
        agent_id="cs_ops",
        documents=(KnowledgeDoc(path="other.md", body="다른 본문"),),
        version="v1",
        synced_at=T1,
    )
    store.put(same_version_but_different)

    got = store.get("cs_ops")
    assert got is not None
    assert got.documents[0].path == "refund.md"


def test_knowledge_get_미등록_agent_id_None(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteKnowledgeStore

    store = SqliteKnowledgeStore(tmp_path / "k.db")
    assert store.get("ghost") is None


def test_knowledge_is_stale_미등록_agent_id_True(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteKnowledgeStore

    store = SqliteKnowledgeStore(tmp_path / "k.db")
    assert store.is_stale("ghost", now=T0, threshold_s=1800) is True


def test_knowledge_is_stale_임계_내외_판정_재오픈_후에도(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteKnowledgeStore

    db = tmp_path / "k.db"
    store = SqliteKnowledgeStore(db)
    store.put(_bundle(synced_at=T0))
    store.close()

    reopened = SqliteKnowledgeStore(db)
    fresh_now = T0 + timedelta(seconds=100)
    stale_now = T0 + timedelta(seconds=3600)
    assert reopened.is_stale("cs_ops", now=fresh_now, threshold_s=1800) is False
    assert reopened.is_stale("cs_ops", now=stale_now, threshold_s=1800) is True


# ── 카드 라이브 등록·오너 변경 durable 저널 리플레이 ─────────────────────────


def _seed_registry() -> Registry:
    reg = Registry()
    reg.register_user(User(id="root_mgr"))
    reg.register_user(User(id="alice", manager="root_mgr"))
    reg.register_user(User(id="bob", manager="root_mgr"))
    reg.register(
        AgentCard(
            agent_id="cs_ops",
            owner="alice",
            team="cs",
            summary="환불 안내",
            domains=["환불"],
            last_reviewed_at=date(2026, 6, 20),
        )
    )
    reg.validate()
    return reg


def test_SqliteRegistryJournal_는_등록_이벤트를_append하고_재오픈_후_보존(
    tmp_path: Path,
) -> None:
    from agent_org_network.sqlite_stores import SqliteRegistryJournal

    db = tmp_path / "j.db"
    journal = SqliteRegistryJournal(db)
    journal.append_register(
        agent_id="new_ops",
        owner="bob",
        team="new",
        summary="새 담당",
        domains=["신규"],
        last_reviewed_at="2026-06-20",
        by="root_mgr",
        at=T0,
    )
    journal.close()

    reopened = SqliteRegistryJournal(db)
    entries = reopened.entries()
    assert len(entries) == 1
    assert entries[0].kind == "register"
    assert entries[0].candidate.agent_id == "new_ops"
    assert entries[0].candidate.owner == "bob"


def test_SqliteRegistryJournal_는_오너변경_이벤트도_append(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteRegistryJournal

    db = tmp_path / "j.db"
    journal = SqliteRegistryJournal(db)
    journal.append_register(
        agent_id="cs_ops",
        owner="alice",
        team="cs",
        summary="환불 안내",
        domains=["환불"],
        last_reviewed_at="2026-06-20",
        by="root_mgr",
        at=T0,
    )
    journal.append_transfer(
        agent_id="cs_ops",
        owner="bob",
        team="cs",
        summary="환불 안내",
        domains=["환불"],
        last_reviewed_at="2026-06-20",
        by="root_mgr",
        at=T1,
    )
    journal.close()

    reopened = SqliteRegistryJournal(db)
    entries = reopened.entries()
    assert [e.kind for e in entries] == ["register", "transfer"]
    assert entries[1].candidate.owner == "bob"


def test_replay_registry_journal_은_등록을_복원한다(tmp_path: Path) -> None:
    from agent_org_network.sqlite_stores import SqliteRegistryJournal
    from agent_org_network.admin_registry import replay_registry_journal

    db = tmp_path / "j.db"
    journal = SqliteRegistryJournal(db)
    journal.append_register(
        agent_id="new_ops",
        owner="bob",
        team="new",
        summary="새 담당",
        domains=["신규"],
        last_reviewed_at="2026-06-20",
        by="root_mgr",
        at=T0,
    )

    reg = _seed_registry()
    assert not reg.has_card("new_ops")
    replay_registry_journal(journal, reg)
    assert reg.has_card("new_ops")
    assert reg.get("new_ops").owner == "bob"


def test_replay_registry_journal_은_오너변경을_반영하고_구owner_아니다(
    tmp_path: Path,
) -> None:
    from agent_org_network.sqlite_stores import SqliteRegistryJournal
    from agent_org_network.admin_registry import replay_registry_journal

    db = tmp_path / "j.db"
    journal = SqliteRegistryJournal(db)
    journal.append_transfer(
        agent_id="cs_ops",
        owner="bob",
        team="cs",
        summary="환불 안내",
        domains=["환불"],
        last_reviewed_at="2026-06-20",
        by="root_mgr",
        at=T0,
    )

    reg = _seed_registry()
    assert reg.get("cs_ops").owner == "alice"
    replay_registry_journal(journal, reg)
    assert reg.get("cs_ops").owner == "bob"
    assert reg.get("cs_ops").owner != "alice"


def test_replay_registry_journal_은_admission_경유_무효_카드_복원_금지(
    tmp_path: Path,
) -> None:
    """리플레이 시에도 admission 검증 경유 — 참조 무결성 깨진(owner 미등록) 저널
    엔트리는 복원되지 않는다(무효 카드는 등록되지 않는다 불변식)."""
    from agent_org_network.sqlite_stores import SqliteRegistryJournal
    from agent_org_network.admin_registry import replay_registry_journal

    db = tmp_path / "j.db"
    journal = SqliteRegistryJournal(db)
    journal.append_register(
        agent_id="ghost_ops",
        owner="nonexistent_user",
        team="x",
        summary="x",
        domains=["신규"],
        last_reviewed_at="2026-06-20",
        by="root_mgr",
        at=T0,
    )

    reg = _seed_registry()
    replay_registry_journal(journal, reg)
    assert not reg.has_card("ghost_ops")


def test_replay_registry_journal_은_순서대로_리플레이한다(tmp_path: Path) -> None:
    """같은 agent_id에 register 후 transfer 순서가 저널 순서 그대로 재현돼야 한다."""
    from agent_org_network.sqlite_stores import SqliteRegistryJournal
    from agent_org_network.admin_registry import replay_registry_journal

    db = tmp_path / "j.db"
    journal = SqliteRegistryJournal(db)
    journal.append_register(
        agent_id="new_ops",
        owner="alice",
        team="new",
        summary="새 담당",
        domains=["신규"],
        last_reviewed_at="2026-06-20",
        by="root_mgr",
        at=T0,
    )
    journal.append_transfer(
        agent_id="new_ops",
        owner="bob",
        team="new",
        summary="새 담당",
        domains=["신규"],
        last_reviewed_at="2026-06-20",
        by="root_mgr",
        at=T1,
    )

    reg = Registry()
    reg.register_user(User(id="root_mgr"))
    reg.register_user(User(id="alice", manager="root_mgr"))
    reg.register_user(User(id="bob", manager="root_mgr"))
    replay_registry_journal(journal, reg)

    assert reg.get("new_ops").owner == "bob"


def test_AdminRegistryService_는_journal_sink_주입시_register_append(
    tmp_path: Path,
) -> None:
    from agent_org_network.sqlite_stores import SqliteRegistryJournal
    from agent_org_network.admin_registry import AdminRegistryService, CardCandidate

    db = tmp_path / "j.db"
    journal = SqliteRegistryJournal(db)
    reg = _seed_registry()
    svc = AdminRegistryService(reg, journal_sink=journal, clock=lambda: T0)
    svc.register_card(
        CardCandidate(
            agent_id="new_ops",
            owner="bob",
            team="new",
            summary="새 담당",
            domains=["신규"],
            last_reviewed_at="2026-06-20",
        ),
        by="root_mgr",
    )

    entries = journal.entries()
    assert len(entries) == 1
    assert entries[0].kind == "register"
    assert entries[0].candidate.agent_id == "new_ops"


def test_AdminRegistryService_는_journal_sink_주입시_transfer_append(
    tmp_path: Path,
) -> None:
    from agent_org_network.sqlite_stores import SqliteRegistryJournal
    from agent_org_network.admin_registry import AdminRegistryService

    db = tmp_path / "j.db"
    journal = SqliteRegistryJournal(db)
    reg = _seed_registry()
    svc = AdminRegistryService(reg, journal_sink=journal, clock=lambda: T0)
    from agent_org_network.admin_registry import CardCandidate

    svc.transfer_ownership(
        CardCandidate(
            agent_id="cs_ops",
            owner="bob",
            team="cs",
            summary="환불 안내",
            domains=["환불"],
            last_reviewed_at="2026-06-20",
        ),
        by="root_mgr",
    )

    entries = journal.entries()
    assert len(entries) == 1
    assert entries[0].kind == "transfer"
    assert entries[0].candidate.owner == "bob"


def test_전체_재기동_시나리오_시드_후_저널_리플레이로_오너변경_복원(
    tmp_path: Path,
) -> None:
    """seed(YAML 동치 Registry) → AdminRegistryService(journal_sink) 통한 오너 변경
    → 프로세스 재시작(새 Registry+새 journal 인스턴스) → replay → 오너 변경 반영."""
    from agent_org_network.sqlite_stores import SqliteRegistryJournal
    from agent_org_network.admin_registry import (
        AdminRegistryService,
        CardCandidate,
        replay_registry_journal,
    )

    db = tmp_path / "j.db"

    # 1차 프로세스: 시드 + 오너 변경.
    journal1 = SqliteRegistryJournal(db)
    reg1 = _seed_registry()
    svc1 = AdminRegistryService(reg1, journal_sink=journal1, clock=lambda: T0)
    svc1.transfer_ownership(
        CardCandidate(
            agent_id="cs_ops",
            owner="bob",
            team="cs",
            summary="환불 안내",
            domains=["환불"],
            last_reviewed_at="2026-06-20",
        ),
        by="root_mgr",
    )
    journal1.close()

    # 재시작: 새 Registry(YAML 시드 동치) + 새 journal 인스턴스로 리플레이.
    reg2 = _seed_registry()
    assert reg2.get("cs_ops").owner == "alice"  # 시드 그대로(오너 변경 전).
    journal2 = SqliteRegistryJournal(db)
    replay_registry_journal(journal2, reg2)

    assert reg2.get("cs_ops").owner == "bob"
