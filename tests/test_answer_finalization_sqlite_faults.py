from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_org_network.answer_finalization import (
    AnswerResponsibilitySnapshot,
    CompletionIdCollisionError,
    IncompleteCompletionStateError,
    ReentrantCompletionMutationError,
)
from agent_org_network.answer_finalization_sqlite import (
    CorruptSqliteCompletionError,
    SqliteCompletionFaultPoint,
    SqliteCompletionStorageUnavailableError,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.approval import (
    ApprovalAssignmentGeneration,
    ApprovalDraft,
    ApprovalItem,
    ApprovalRequired,
    Approve,
    ApprovedCandidate,
    AnswerCandidate,
    FinalizationCandidate,
    InMemoryApprovalStore,
    NoApprovalRequired,
)
from agent_org_network.question_request import (
    AwaitingApproval,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.runtime import AnswerMode
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema

NOW = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)


class _Policy:
    def evaluate(
        self,
        org_id: str,
        route: RouteTarget,
        candidate_mode: AnswerMode,
    ) -> NoApprovalRequired:
        return NoApprovalRequired(policy_version="approval-v1")


class _Resolver:
    def resolve(
        self,
        *,
        org_id: str,
        route: RouteTarget,
    ) -> AnswerResponsibilitySnapshot:
        return AnswerResponsibilitySnapshot(agent_id=route.agent_id, owner_id="owner-1")


class _FailAt:
    def __init__(self, target: SqliteCompletionFaultPoint) -> None:
        self.target = target
        self.calls: list[SqliteCompletionFaultPoint] = []

    def __call__(self, point: SqliteCompletionFaultPoint) -> None:
        self.calls.append(point)
        if point == self.target:
            raise RuntimeError(f"injected:{point}")


def _store(
    path: Path,
    *,
    fault: _FailAt | None = None,
    record_id: str = "record-1",
    timeout: float = 5.0,
) -> SqliteQuestionCompletionUnitOfWork:
    return SqliteQuestionCompletionUnitOfWork(
        path,
        policy=_Policy(),
        approvals=InMemoryApprovalStore(),
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: record_id,
        clock=lambda: NOW,
        fault_injector=fault,
        timeout=timeout,
    )


def _ready(
    store: SqliteQuestionCompletionUnitOfWork,
    *,
    session_id: str | None,
    requires_approval: bool = False,
) -> QuestionRequest:
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        question="답변은 언제 오나요?",
        request_id_factory=lambda: "req-1",
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
        session_id=session_id,
    )
    store.create(received)
    route = RouteTarget(
        intent="answer_eta",
        agent_id="support-card",
        requires_approval=requires_approval,
        authority_version="route-v1",
    )
    trigger = "request-dispatch:req-1:1"
    ready = received.record_initial_routing(
        intent=route.intent,
        disposition="routed",
        target=ReadyToDispatch(
            route=route,
            attempt=1,
            trigger_key=trigger,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger,
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW - timedelta(minutes=1),
    )
    assert store.compare_and_set("req-1", 0, received, ready)
    return ready


def _handoff(ready: QuestionRequest) -> FinalizationCandidate:
    assert isinstance(ready.state, ReadyToDispatch)
    return FinalizationCandidate(
        request_id=ready.request_id,
        expected_revision=ready.revision,
        attempt=ready.state.attempt,
        route=ready.state.route,
        candidate=AnswerCandidate(text="10분 안에 답변합니다.", mode="full"),
        approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
    )


def _artifact_counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "answer_records",
                "terminal_answer_audits",
                "request_session_turns",
                "question_delivery_outbox",
                "question_completion_receipts",
            )
        }


@pytest.mark.parametrize(
    "point",
    [
        "after_answer_record",
        "after_request",
        "after_audit",
        "after_session",
        "after_outbox",
        "after_completion_receipt",
        "before_commit",
    ],
)
@pytest.mark.parametrize("session_id", [None, "session-1"])
def test_모든_SQLite_completion_fault는_Request와_artifact를_전부_rollback한다(
    tmp_path: Path,
    point: SqliteCompletionFaultPoint,
    session_id: str | None,
) -> None:
    path = tmp_path / f"{point}-{session_id}.db"
    migrate_sqlite_completion_schema(path)
    fault = _FailAt(point)
    store = _store(path, fault=fault)
    ready = _ready(store, session_id=session_id)

    with pytest.raises(RuntimeError, match=f"injected:{point}"):
        store.complete(_handoff(ready))

    assert store.get(ready.request_id) == ready
    assert store.by_request(ready.request_id) is None
    assert _artifact_counts(path) == {
        "answer_records": 0,
        "terminal_answer_audits": 0,
        "request_session_turns": 0,
        "question_delivery_outbox": 0,
        "question_completion_receipts": 0,
    }


class _ReentrantFault:
    def __init__(self) -> None:
        self.store: SqliteQuestionCompletionUnitOfWork | None = None

    def __call__(self, point: SqliteCompletionFaultPoint) -> None:
        if point != "after_request":
            return
        assert self.store is not None
        other = QuestionRequest.receive(
            org_id="org-1",
            requester_id="user-2",
            question="재진입 질문",
            request_id_factory=lambda: "req-2",
            clock=lambda: NOW,
            due_at=NOW + timedelta(hours=1),
        )
        self.store.create(other)


def test_finalization_callback의_같은_UoW_write_재진입을_거부하고_rollback한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reentrant.db"
    migrate_sqlite_completion_schema(path)
    fault = _ReentrantFault()
    store = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=_Policy(),
        approvals=InMemoryApprovalStore(),
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "record-1",
        clock=lambda: NOW,
        fault_injector=fault,
    )
    fault.store = store
    ready = _ready(store, session_id=None)

    with pytest.raises(ReentrantCompletionMutationError):
        store.complete(_handoff(ready))

    assert store.get("req-1") == ready
    assert store.get("req-2") is None
    assert _artifact_counts(path) == dict.fromkeys(_artifact_counts(path), 0)


def test_legacy_record_ID_collision을_명시적_domain_error로_분류한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "collision.db"
    migrate_sqlite_completion_schema(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO answer_records ("
            "record_id, question, answer_text, answered_by, agent_id, mode, session_id, "
            "answered_at, needs_correction_review, request_id, sources_json, snapshot_sha"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "collision-record",
                "legacy 질문",
                "legacy 답",
                "owner",
                "card",
                "full",
                None,
                NOW.isoformat(),
                0,
                None,
                None,
                None,
            ),
        )
    store = _store(path, record_id="collision-record")
    ready = _ready(store, session_id=None)

    with pytest.raises(CompletionIdCollisionError):
        store.complete(_handoff(ready))

    assert store.get("req-1") == ready
    assert store.by_request("req-1") is None
    counts = _artifact_counts(path)
    assert counts["answer_records"] == 1
    assert sum(value for key, value in counts.items() if key != "answer_records") == 0


def test_SQLite_lock_timeout을_domain_concurrency가_아닌_storage_unavailable로_분류한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "locked.db"
    migrate_sqlite_completion_schema(path)
    store = _store(path, timeout=0.01)
    ready = _ready(store, session_id=None)
    blocker = sqlite3.connect(path)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(SqliteCompletionStorageUnavailableError):
            store.complete(_handoff(ready))
    finally:
        blocker.rollback()
        blocker.close()

    assert store.get("req-1") == ready
    assert store.by_request("req-1") is None


@pytest.mark.parametrize(
    ("sql", "value"),
    [
        ("UPDATE question_completion_receipts SET handoff_kind = ?", "unknown"),
        ("UPDATE question_completion_receipts SET handoff_schema_version = ?", 3),
        ("UPDATE question_completion_receipts SET handoff_sha256 = ?", "0" * 64),
        ("UPDATE question_completion_receipts SET record_id = ?", "other-record"),
        ("UPDATE question_completion_receipts SET request_id = ?", "other-request"),
        (
            "UPDATE question_completion_receipts SET handoff_json = ?",
            '{"kind":"finalization_candidate","kind":"finalization_candidate"}',
        ),
        (
            "UPDATE question_completion_receipts SET handoff_json = ?",
            '{"kind":"finalization_candidate","attempt":NaN}',
        ),
        (
            "UPDATE question_completion_receipts SET handoff_json = ?",
            '{ "kind": "finalization_candidate" }',
        ),
        ("UPDATE question_completion_receipts SET created_at = ?", "2026-07-13T10:31:00+00:00"),
        ("UPDATE terminal_answer_audits SET audit_schema_version = ?", 2),
        (
            "UPDATE terminal_answer_audits SET route_json = ?",
            '{"agent_id":"card","agent_id":"other"}',
        ),
        (
            "UPDATE terminal_answer_audits SET route_json = ?",
            '{"agent_id":NaN}',
        ),
        (
            "UPDATE terminal_answer_audits SET route_json = ?",
            '{ "agent_id": "card" }',
        ),
        (
            "UPDATE terminal_answer_audits SET route_json = ?",
            '{"agent_id":"other-card","authority_version":"route-v1",'
            '"intent":"answer_eta","requires_approval":false}',
        ),
        (
            "UPDATE terminal_answer_audits SET responsibility_json = ?",
            '{"agent_id":"card","owner_id":"owner","owner_id":"other"}',
        ),
        (
            "UPDATE terminal_answer_audits SET responsibility_json = ?",
            '{"agent_id":"support-card","owner_id":"other-owner"}',
        ),
        (
            "UPDATE terminal_answer_audits SET approval_json = ?",
            '{"kind":"not_required","policy_version":NaN}',
        ),
        (
            "UPDATE terminal_answer_audits SET approval_json = ?",
            '{"kind":"not_required","policy_version":"approval-v2"}',
        ),
        ("UPDATE terminal_answer_audits SET record_id = ?", "other-record"),
        ("UPDATE terminal_answer_audits SET request_id = ?", "other-request"),
        ("UPDATE terminal_answer_audits SET candidate_mode = ?", "invalid"),
        ("UPDATE terminal_answer_audits SET final_mode = ?", "draft_only"),
        ("UPDATE terminal_answer_audits SET org_id = ?", "other-org"),
        ("UPDATE terminal_answer_audits SET completed_at = ?", "2026-07-13T10:31:00+00:00"),
        ("UPDATE answer_records SET sources_json = ?", None),
        ("UPDATE answer_records SET sources_json = ?", '[ "source" ]'),
        ("UPDATE answer_records SET sources_json = ?", '[""]'),
        ("UPDATE answer_records SET record_id = ?", "other-record"),
        ("UPDATE answer_records SET request_id = ?", "other-request"),
        ("UPDATE answer_records SET answer_text = ?", "변조된 답"),
        ("UPDATE answer_records SET answered_by = ?", "other-owner"),
        ("UPDATE answer_records SET agent_id = ?", "other-card"),
        ("UPDATE answer_records SET mode = ?", "invalid"),
        ("UPDATE answer_records SET answered_at = ?", "2026-07-13T10:31:00+00:00"),
        ("UPDATE answer_records SET needs_correction_review = ?", 1),
        ("UPDATE request_session_turns SET question = ?", "다른 질문"),
        ("UPDATE request_session_turns SET record_id = ?", "other-record"),
        ("UPDATE request_session_turns SET request_id = ?", "other-request"),
        ("UPDATE request_session_turns SET session_id = ?", "other-session"),
        ("UPDATE question_delivery_outbox SET kind = ?", "other_kind"),
        ("UPDATE question_delivery_outbox SET record_id = ?", "other-record"),
        ("UPDATE question_delivery_outbox SET request_id = ?", "other-request"),
        ("UPDATE question_delivery_outbox SET created_at = ?", "2026-07-13T10:31:00+00:00"),
        ("UPDATE question_requests SET updated_at = ?", "2026-07-13T10:31:00+00:00"),
        ("UPDATE question_requests SET intent = ?", "other-intent"),
    ],
)
def test_각_completion_artifact_변조를_strict_reader가_fail_closed한다(
    tmp_path: Path,
    sql: str,
    value: object,
) -> None:
    path = tmp_path / "corrupt.db"
    migrate_sqlite_completion_schema(path)
    store = _store(path)
    ready = _ready(store, session_id="session-1")
    store.complete(_handoff(ready))
    with sqlite3.connect(path) as connection:
        connection.execute(sql, (value,))

    with pytest.raises(IncompleteCompletionStateError):
        store.by_request(ready.request_id)


@pytest.mark.parametrize(
    "table",
    [
        "answer_records",
        "terminal_answer_audits",
        "request_session_turns",
        "question_delivery_outbox",
        "question_completion_receipts",
    ],
)
def test_completion_artifact_행_누락을_reader가_fail_closed한다(
    tmp_path: Path,
    table: str,
) -> None:
    path = tmp_path / f"missing-{table}.db"
    migrate_sqlite_completion_schema(path)
    store = _store(path)
    ready = _ready(store, session_id="session-1")
    completion = store.complete(_handoff(ready))
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(f"DELETE FROM {table}")

    with pytest.raises(IncompleteCompletionStateError):
        store.by_request(ready.request_id)
    if table == "question_completion_receipts":
        with pytest.raises(IncompleteCompletionStateError):
            store.by_record(completion.record_id)


def test_receipt의_canonical_JSON과_digest를_함께_바꿔도_semantic_mismatch를_거부한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "semantic-receipt.db"
    migrate_sqlite_completion_schema(path)
    store = _store(path)
    ready = _ready(store, session_id=None)
    store.complete(_handoff(ready))
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT handoff_json FROM question_completion_receipts").fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["candidate"]["text"] = "receipt만 바꾼 답"
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        connection.execute(
            "UPDATE question_completion_receipts SET handoff_json = ?, handoff_sha256 = ?",
            (raw, digest),
        )

    with pytest.raises(IncompleteCompletionStateError):
        store.by_request(ready.request_id)


def test_schema_drift_OperationalError를_availability가_아닌_corruption으로_분류한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schema-drift.db"
    migrate_sqlite_completion_schema(path)
    store = _store(path)
    ready = _ready(store, session_id=None)
    store.complete(_handoff(ready))
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE question_delivery_outbox")

    with pytest.raises(CorruptSqliteCompletionError):
        store.by_request(ready.request_id)


@pytest.mark.parametrize("dependency", ["policy", "resolver", "record_id", "clock"])
def test_planner_dependency_callback의_같은_UoW_write_재진입을_거부한다(
    tmp_path: Path,
    dependency: str,
) -> None:
    path = tmp_path / f"dependency-{dependency}.db"
    migrate_sqlite_completion_schema(path)
    store_ref: list[SqliteQuestionCompletionUnitOfWork] = []

    def reenter() -> None:
        other = QuestionRequest.receive(
            org_id="org-1",
            requester_id="user-2",
            question="planner 재진입",
            request_id_factory=lambda: "req-2",
            clock=lambda: NOW,
            due_at=NOW + timedelta(hours=1),
        )
        store_ref[0].create(other)

    class ReentrantPolicy(_Policy):
        def evaluate(
            self,
            org_id: str,
            route: RouteTarget,
            candidate_mode: AnswerMode,
        ) -> NoApprovalRequired:
            if dependency == "policy":
                reenter()
            return super().evaluate(org_id, route, candidate_mode)

    class ReentrantResolver(_Resolver):
        def resolve(
            self,
            *,
            org_id: str,
            route: RouteTarget,
        ) -> AnswerResponsibilitySnapshot:
            if dependency == "resolver":
                reenter()
            return super().resolve(org_id=org_id, route=route)

    def new_record_id() -> str:
        if dependency == "record_id":
            reenter()
        return "record-1"

    def clock() -> datetime:
        if dependency == "clock":
            reenter()
        return NOW

    store = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=ReentrantPolicy(),
        approvals=InMemoryApprovalStore(),
        responsibility_resolver=ReentrantResolver(),
        record_id_factory=new_record_id,
        clock=clock,
    )
    store_ref.append(store)
    ready = _ready(store, session_id=None)

    with pytest.raises(ReentrantCompletionMutationError):
        store.complete(_handoff(ready))

    assert store.get(ready.request_id) == ready
    assert store.get("req-2") is None
    assert store.by_request(ready.request_id) is None


class _ReentrantApprovalStore(InMemoryApprovalStore):
    def __init__(self) -> None:
        super().__init__()
        self.store: SqliteQuestionCompletionUnitOfWork | None = None
        self.enabled = False

    def get(self, item_id: str) -> ApprovalItem | None:
        if self.enabled:
            assert self.store is not None
            other = QuestionRequest.receive(
                org_id="org-1",
                requester_id="user-2",
                question="Approval Store 재진입",
                request_id_factory=lambda: "req-2",
                clock=lambda: NOW,
                due_at=NOW + timedelta(hours=1),
            )
            self.store.create(other)
        return super().get(item_id)


def test_Approval_Store_callback의_같은_UoW_write_재진입을_거부한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approval-store-reentrant.db"
    migrate_sqlite_completion_schema(path)
    approvals = _ReentrantApprovalStore()
    store = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=_Policy(),
        approvals=approvals,
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "record-1",
        clock=lambda: NOW,
    )
    approvals.store = store
    ready = _ready(store, session_id=None, requires_approval=True)
    assert isinstance(ready.state, ReadyToDispatch)
    item_id = "approval-1"
    awaiting = ready.transition(
        AwaitingApproval(
            route=ready.state.route,
            attempt=1,
            draft_ref=item_id,
            handling=HandlingAssignment(
                kind="approval_item",
                ref=item_id,
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW - timedelta(seconds=30),
    )
    assert store.compare_and_set(ready.request_id, ready.revision, ready, awaiting)
    candidate = AnswerCandidate(text="승인할 답", mode="full")
    draft = ApprovalDraft(
        draft_id="draft-1",
        request_id=ready.request_id,
        attempt=1,
        route=ready.state.route,
        candidate=candidate,
        created_at=NOW - timedelta(seconds=20),
    )
    requirement = ApprovalRequired(
        approver_id="approver-1",
        policy_version="approval-v1",
    )
    item = ApprovalItem(
        item_id=item_id,
        org_id=ready.org_id,
        request_id=ready.request_id,
        awaiting_revision=awaiting.revision,
        attempt=1,
        route=ready.state.route,
        draft=draft,
        requirement=requirement,
        created_at=draft.created_at,
        due_at=NOW + timedelta(hours=1),
    )
    approved = ApprovedCandidate(
        request_id=ready.request_id,
        item_id=item_id,
        expected_revision=awaiting.revision,
        attempt=1,
        route=ready.state.route,
        candidate=candidate,
        approved_by="approver-1",
        approved_at=NOW - timedelta(seconds=10),
        edited=False,
        policy_version="approval-v1",
        assignment_generation=ApprovalAssignmentGeneration.from_item(item),
    )
    action = Approve(by_approver="approver-1")
    approvals.create_or_get(item)
    approvals.resolve_if_open(
        item_id,
        action,
        lambda current: current.resolve(
            action=action,
            approved_candidate=approved,
            resolved_at=approved.approved_at,
        ),
    )
    approvals.enabled = True

    with pytest.raises(ReentrantCompletionMutationError):
        store.complete(approved)

    assert store.get(ready.request_id) == awaiting
    assert store.get("req-2") is None
    assert store.by_request(ready.request_id) is None


def test_receipt없는_request_aware_AnswerRecord도_새_completion을_막는다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "residual-answer.db"
    migrate_sqlite_completion_schema(path)
    store = _store(path, record_id="residual-record")
    ready = _ready(store, session_id=None)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO answer_records ("
            "record_id, question, answer_text, answered_by, agent_id, mode, session_id, "
            "answered_at, needs_correction_review, request_id, sources_json, snapshot_sha"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "residual-record",
                ready.question,
                "legacy correlated 답",
                "owner",
                "support-card",
                "full",
                None,
                NOW.isoformat(),
                0,
                ready.request_id,
                "[]",
                None,
            ),
        )

    with pytest.raises(IncompleteCompletionStateError):
        store.complete(_handoff(ready))

    assert store.get(ready.request_id) == ready
    with pytest.raises(IncompleteCompletionStateError):
        store.by_request(ready.request_id)


def test_receipt없는_v2_흔적은_planner_callback보다_먼저_거부한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "residual-before-planner.db"
    migrate_sqlite_completion_schema(path)

    class NeverPolicy(_Policy):
        def evaluate(
            self,
            org_id: str,
            route: RouteTarget,
            candidate_mode: AnswerMode,
        ) -> NoApprovalRequired:
            raise AssertionError("policy callback must not run")

    class NeverResolver(_Resolver):
        def resolve(
            self,
            *,
            org_id: str,
            route: RouteTarget,
        ) -> AnswerResponsibilitySnapshot:
            raise AssertionError("resolver callback must not run")

    store = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=NeverPolicy(),
        approvals=InMemoryApprovalStore(),
        responsibility_resolver=NeverResolver(),
        record_id_factory=lambda: (_ for _ in ()).throw(
            AssertionError("record ID callback must not run")
        ),
        clock=lambda: (_ for _ in ()).throw(AssertionError("clock callback must not run")),
    )
    ready = _ready(store, session_id=None)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO answer_records ("
            "record_id, question, answer_text, answered_by, agent_id, mode, session_id, "
            "answered_at, needs_correction_review, request_id, sources_json, snapshot_sha"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "residual-record",
                ready.question,
                "receipt 없는 v2 답",
                "owner",
                "support-card",
                "full",
                None,
                NOW.isoformat(),
                0,
                ready.request_id,
                "[]",
                None,
            ),
        )

    with pytest.raises(IncompleteCompletionStateError):
        store.complete(_handoff(ready))
