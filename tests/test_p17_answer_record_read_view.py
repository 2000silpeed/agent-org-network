"""P17 Finalization AnswerRecord와 기존 감독 읽기 경계 통합 회귀."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.answer_record import (
    AnswerRecord,
    AnswerRecordReadCollisionError,
    CompositeAnswerRecordReader,
    InMemoryAnswerRecordStore,
    InMemoryCorrectionStore,
    InMemoryFeedbackStore,
)
from agent_org_network.answer_finalization import (
    IncompleteCompletionStateError,
    ResponsibilitySnapshotResolver,
)
from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.approval import ApprovalPolicy, ApprovalStore
from agent_org_network.demo import build_demo
from agent_org_network.demo_question_surfaces import build_demo_question_surface_composition
from agent_org_network.question_surface_composition import AtomicQuestionCompletionStorage
from agent_org_network.runtime import StubRuntime
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_stores import SqliteAnswerRecordStore
from agent_org_network.web import create_app


def _post(client: TestClient, path: str, payload: dict[str, object]) -> Response:
    http: Any = client
    return cast(Response, http.post(path, json=payload))


def _get(client: TestClient, path: str, **kwargs: object) -> Response:
    http: Any = client
    return cast(Response, http.get(path, **kwargs))


def _legacy_record() -> AnswerRecord:
    return AnswerRecord(
        record_id="legacy-record-1",
        question="기존 질문",
        answer_text="기존 답변",
        answered_by="cs_lead",
        agent_id="cs_ops",
        mode="full",
        session_id=None,
        answered_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )


class _CompletionRecords:
    def __init__(self, records: list[AnswerRecord]) -> None:
        self._records = records

    def answer_record(self, record_id: str) -> AnswerRecord | None:
        return next((record for record in self._records if record.record_id == record_id), None)

    def answer_records_for_agent(self, agent_id: str) -> list[AnswerRecord]:
        return [record for record in self._records if record.agent_id == agent_id]


def _sqlite_composition(path: Path):
    migrate_sqlite_completion_schema(path)
    bundle = build_demo(runtime=StubRuntime())
    record_ids = iter(("sqlite-record-1", "sqlite-record-2", "sqlite-record-3"))

    def storage_factory(
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage:
        return SqliteQuestionCompletionUnitOfWork(
            path,
            policy=policy,
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
            record_id_factory=lambda: next(record_ids),
            clock=lambda: datetime.now(timezone.utc),
        )

    return build_demo_question_surface_composition(
        bundle,
        storage_factory=storage_factory,
    )


def test_P17_답에_feedback을_남기면_감독_목록에_bad가_보인다() -> None:
    feedback = InMemoryFeedbackStore()
    app = create_app(runtime=StubRuntime(), feedback_store=feedback)

    with TestClient(app) as client:
        answered = _post(client, "/ask", {"question": "환불은 언제 되나요?"})
        body = cast(dict[str, object], answered.json())
        record_id = cast(str, body["record_id"])

        submitted = _post(
            client,
            f"/answer/{record_id}/feedback",
            {"verdict": "bad", "comment": "부정확해요"},
        )
        supervision = _get(
            client,
            "/supervision/answers",
            params={"agent_id": "cs_ops"},
        )

    assert submitted.status_code == 200
    items = cast(list[dict[str, object]], supervision.json())
    assert [item["record_id"] for item in items] == [record_id]
    assert cast(dict[str, object], items[0]["feedback"])["verdict"] == "bad"
    assert items[0]["needs_correction_review"] is True


def test_P17_답을_정정하면_질문자_정정_view에_보인다() -> None:
    corrections = InMemoryCorrectionStore()
    app = create_app(runtime=StubRuntime(), correction_store=corrections)

    with TestClient(app) as client:
        answered = _post(client, "/ask", {"question": "환불은 언제 되나요?"})
        body = cast(dict[str, object], answered.json())
        record_id = cast(str, body["record_id"])
        owner = cast(dict[str, object], body["answered_by"])["owner"]

        corrected = _post(
            client,
            f"/supervision/answers/{record_id}/correct",
            {"by_owner": owner, "corrected_text": "정정된 환불 안내"},
        )
        view = _get(client, f"/answer/{record_id}/correction")

    assert corrected.status_code == 200
    assert view.status_code == 200
    assert view.json() == {
        "record_id": record_id,
        "original_text": body["text"],
        "has_correction": True,
        "corrected_text": "정정된 환불 안내",
        "corrected_at": view.json()["corrected_at"],
    }


def test_주입된_legacy_AnswerRecord도_P17_답과_함께_조회된다() -> None:
    legacy = InMemoryAnswerRecordStore()
    legacy.add(_legacy_record())
    app = create_app(runtime=StubRuntime(), answer_record_store=legacy)

    with TestClient(app) as client:
        answered = _post(client, "/ask", {"question": "환불은 언제 되나요?"})
        record_id = cast(str, cast(dict[str, object], answered.json())["record_id"])
        supervision = _get(
            client,
            "/supervision/answers",
            params={"agent_id": "cs_ops"},
        )

    ids = {
        cast(str, item["record_id"]) for item in cast(list[dict[str, object]], supervision.json())
    }
    assert ids == {"legacy-record-1", record_id}
    assert legacy.get(record_id) is None


def test_InMemory_completion_read_view는_단건과_카드_목록을_exact로_돌려준다() -> None:
    app = create_app(runtime=StubRuntime())
    composition = app.state.question_surface_composition

    with TestClient(app) as client:
        answered = cast(dict[str, object], _post(client, "/ask", {"question": "환불 문의"}).json())

    record_id = cast(str, answered["record_id"])
    record = composition.answer_records.answer_record(record_id)
    records = composition.answer_records.answer_records_for_agent("cs_ops")
    assert record is not None
    assert record.record_id == record_id
    assert records == [record]


def test_InMemory_completion_단건_상관키_손상은_legacy로_fallback하지_않는다() -> None:
    app = create_app(runtime=StubRuntime())
    composition = app.state.question_surface_composition

    with TestClient(app) as client:
        answered = cast(dict[str, object], _post(client, "/ask", {"question": "환불 문의"}).json())

    record_id = cast(str, answered["record_id"])
    state = composition.storage._state
    object.__setattr__(state.records_by_id[record_id], "request_id", None)

    with pytest.raises(IncompleteCompletionStateError):
        composition.answer_records.answer_record(record_id)


def test_InMemory_native_record_누락은_legacy_동일_ID로_fallback하지_않는다() -> None:
    legacy = InMemoryAnswerRecordStore()
    app = create_app(runtime=StubRuntime(), answer_record_store=legacy)
    composition = app.state.question_surface_composition
    reader = app.state.answer_record_view

    with TestClient(app) as client:
        answered = cast(dict[str, object], _post(client, "/ask", {"question": "환불 문의"}).json())

    record_id = cast(str, answered["record_id"])
    native = composition.answer_records.answer_record(record_id)
    assert native is not None
    legacy.add(native.model_copy(update={"request_id": None}))
    composition.storage._state.records_by_id.pop(record_id)

    with pytest.raises(IncompleteCompletionStateError):
        reader.get(record_id)


def test_InMemory_completion_목록은_다른_카드_artifact_손상도_부분_결과로_숨기지_않는다() -> None:
    app = create_app(runtime=StubRuntime())
    composition = app.state.question_surface_composition

    with TestClient(app) as client:
        refund = cast(dict[str, object], _post(client, "/ask", {"question": "환불 문의"}).json())
        contract = cast(
            dict[str, object],
            _post(client, "/ask", {"question": "계약 조건 검토"}).json(),
        )

    state = composition.storage._state
    state.record_id_by_request.pop(cast(str, contract["request_id"]))

    with pytest.raises(IncompleteCompletionStateError):
        composition.answer_records.answer_records_for_agent("cs_ops")
    assert refund["record_id"] != contract["record_id"]


def test_composite_read_view는_동일_기록은_합치고_상이한_ID_충돌은_거부한다() -> None:
    record = _legacy_record()
    legacy = InMemoryAnswerRecordStore()
    legacy.add(record)
    deduplicated = CompositeAnswerRecordReader(legacy, _CompletionRecords([record]))

    assert deduplicated.get(record.record_id) == record
    assert deduplicated.for_agent(record.agent_id) == [record]

    conflicting = record.model_copy(update={"answer_text": "다른 답변"})
    collision = CompositeAnswerRecordReader(legacy, _CompletionRecords([conflicting]))
    with pytest.raises(AnswerRecordReadCollisionError):
        collision.get(record.record_id)
    with pytest.raises(AnswerRecordReadCollisionError):
        collision.for_agent(record.agent_id)


def test_SQLite_completion_read_view는_legacy_같은_행을_중복_노출하지_않는다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "completion.db"
    composition = _sqlite_composition(path)
    legacy = SqliteAnswerRecordStore(path)
    app = create_app(
        runtime=StubRuntime(),
        answer_record_store=legacy,
        question_surface_composition=composition,
    )

    with TestClient(app) as client:
        answered = cast(dict[str, object], _post(client, "/ask", {"question": "환불 문의"}).json())
        supervision = cast(
            list[dict[str, object]],
            _get(client, "/supervision/answers", params={"agent_id": "cs_ops"}).json(),
        )
        stored = composition.answer_records.answer_record("sqlite-record-1")

    assert answered["record_id"] == "sqlite-record-1"
    assert [item["record_id"] for item in supervision] == ["sqlite-record-1"]
    assert stored is not None
    legacy.close()


def test_SQLite_completion_receipt_손상은_빈_목록으로_숨지_않는다(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    composition = _sqlite_composition(path)
    app = create_app(
        runtime=StubRuntime(),
        question_surface_composition=composition,
    )

    with TestClient(app) as client:
        refund = cast(dict[str, object], _post(client, "/ask", {"question": "환불 문의"}).json())
        contract = cast(
            dict[str, object],
            _post(client, "/ask", {"question": "계약 조건 검토"}).json(),
        )

        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "DELETE FROM question_completion_receipts WHERE request_id = ?",
            (contract["request_id"],),
        )
        connection.commit()
        connection.close()

        with pytest.raises(IncompleteCompletionStateError):
            composition.answer_records.answer_records_for_agent("cs_ops")
        assert refund["record_id"] != contract["record_id"]


def test_SQLite_AnsweredRequest만_남은_손상은_legacy_동일_ID로_fallback하지_않는다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "answered-only.db"
    composition = _sqlite_composition(path)
    legacy = InMemoryAnswerRecordStore()
    app = create_app(
        runtime=StubRuntime(),
        answer_record_store=legacy,
        question_surface_composition=composition,
    )

    with TestClient(app) as client:
        answered = cast(dict[str, object], _post(client, "/ask", {"question": "환불 문의"}).json())
        record_id = cast(str, answered["record_id"])
        native = composition.answer_records.answer_record(record_id)
        assert native is not None
        legacy.add(native.model_copy(update={"request_id": None}))

        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys = OFF")
        for table in (
            "question_completion_receipts",
            "terminal_answer_audits",
            "question_delivery_outbox",
            "request_session_turns",
            "answer_records",
        ):
            connection.execute(f"DELETE FROM {table} WHERE record_id = ?", (record_id,))
        connection.commit()
        connection.close()

        with pytest.raises(IncompleteCompletionStateError):
            app.state.answer_record_view.get(record_id)
