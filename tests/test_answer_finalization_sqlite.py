from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_org_network.answer_finalization import (
    AnswerResponsibilitySnapshot,
    CompletionConcurrencyError,
    DirectAnsweredTransitionError,
    IncompleteCompletionStateError,
)
from agent_org_network.answer_finalization_sqlite import (
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.approval import (
    ApprovalAssignmentGeneration,
    ApprovalDraft,
    ApprovalItem,
    ApprovalRequired,
    ApproveWithEdit,
    ApprovedCandidate,
    AnswerCandidate,
    FinalizationCandidate,
    InMemoryApprovalStore,
    NoApprovalRequired,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingApproval,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.runtime import AnswerMode
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema

NOW = datetime(2026, 7, 13, 18, 12, 11, 987654, tzinfo=timezone(timedelta(hours=9)))


class _Policy:
    def __init__(self, result: NoApprovalRequired | ApprovalRequired) -> None:
        self.result = result
        self.calls = 0

    def evaluate(
        self,
        org_id: str,
        route: RouteTarget,
        candidate_mode: AnswerMode,
    ) -> NoApprovalRequired | ApprovalRequired:
        self.calls += 1
        return self.result


class _Resolver:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(
        self,
        *,
        org_id: str,
        route: RouteTarget,
    ) -> AnswerResponsibilitySnapshot | None:
        self.calls += 1
        return AnswerResponsibilitySnapshot(
            agent_id=route.agent_id,
            owner_id="담당자-user-1",
        )


def _open(
    path: Path,
    *,
    policy: _Policy | None = None,
    approvals: InMemoryApprovalStore | None = None,
    resolver: _Resolver | None = None,
    record_id_factory: Callable[[], str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> SqliteQuestionCompletionUnitOfWork:
    return SqliteQuestionCompletionUnitOfWork(
        path,
        policy=policy or _Policy(NoApprovalRequired(policy_version="approval-v1")),
        approvals=approvals or InMemoryApprovalStore(),
        responsibility_resolver=resolver or _Resolver(),
        record_id_factory=record_id_factory or (lambda: "기록-record-1"),
        clock=clock or (lambda: NOW),
    )


def _ready(
    store: SqliteQuestionCompletionUnitOfWork,
    *,
    request_id: str = "요청-req-1",
    session_id: str | None = "세션-session-1",
    requires_approval: bool = False,
) -> QuestionRequest:
    received = QuestionRequest.receive(
        org_id="조직-org-1",
        requester_id="질문자-user-1",
        question="환불은 언제 처리되나요? 💳",
        request_id_factory=lambda: request_id,
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
        session_id=session_id,
        context_snapshot="서울 고객 · VIP",
    )
    store.create(received)
    route = RouteTarget(
        intent="환불-refund",
        agent_id="환불-card-1",
        requires_approval=requires_approval,
        authority_version="route-v1",
    )
    trigger = f"request-dispatch:{request_id}:1"
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
    assert store.compare_and_set(request_id, 0, received, ready)
    return ready


def _handoff(
    request: QuestionRequest,
    *,
    text: str = "영업일 기준 3일 안에 처리됩니다. ✅",
    sources: tuple[str, ...] = ("환불정책.md", "SLA-2026.md"),
    snapshot_sha: str | None = "snapshot-한글-1",
    needs_correction_review: bool = False,
) -> FinalizationCandidate:
    assert isinstance(request.state, ReadyToDispatch)
    return FinalizationCandidate(
        request_id=request.request_id,
        expected_revision=request.revision,
        attempt=request.state.attempt,
        route=request.state.route,
        candidate=AnswerCandidate(
            text=text,
            sources=sources,
            mode="full",
            snapshot_sha=snapshot_sha,
        ),
        approval_evaluation=NoApprovalRequired(
            policy_version="approval-v1",
            needs_correction_review=needs_correction_review,
        ),
    )


@pytest.fixture
def capable_path(tmp_path: Path) -> Path:
    path = tmp_path / "completion.db"
    migrate_sqlite_completion_schema(path)
    return path


def test_sqlite_completion은_모든_artifact를_한_bundle로_복원하고_reopen한다(
    capable_path: Path,
) -> None:
    store = _open(capable_path)
    ready = _ready(store)
    handoff = _handoff(ready)

    completion = store.complete(handoff)
    bundle = store.by_request(ready.request_id)

    assert bundle is not None
    assert bundle.completion == completion
    assert bundle.answer_record.sources == ("환불정책.md", "SLA-2026.md")
    assert bundle.answer_record.snapshot_sha == "snapshot-한글-1"
    assert bundle.completion.completed_at == NOW
    assert bundle.session_turn is not None
    assert bundle.session_turn.request_id == ready.request_id
    assert store.by_record(completion.record_id) == bundle
    assert store.get(ready.request_id) == bundle.request
    assert isinstance(bundle.request.state, AnsweredRequest)
    assert store.nonterminal() == []
    store.close()

    reopened = _open(
        capable_path,
        policy=_ExplodingPolicy(),
        resolver=_ExplodingResolver(),
        record_id_factory=lambda: (_ for _ in ()).throw(AssertionError("record factory")),
        clock=lambda: (_ for _ in ()).throw(AssertionError("clock")),
    )
    assert reopened.by_request(ready.request_id) == bundle
    assert reopened.by_record(completion.record_id) == bundle
    # receipt exact equality를 planner callback보다 먼저 판정한다.
    assert reopened.complete(handoff) == completion
    reopened.close()


def test_SQLite가_true_사후교정_증거를_restart_복원하고_변조는_fail_closed한다(
    capable_path: Path,
) -> None:
    store = _open(
        capable_path,
        policy=_Policy(
            NoApprovalRequired(
                policy_version="approval-v1",
                needs_correction_review=True,
            )
        ),
    )
    ready = _ready(store, session_id=None)
    store.complete(_handoff(ready, needs_correction_review=True))
    bundle = store.by_request(ready.request_id)

    assert bundle is not None
    assert bundle.answer_record.needs_correction_review is True
    assert bundle.terminal_audit.responsibility.needs_correction_review is True
    store.close()

    with sqlite3.connect(capable_path) as connection:
        evidence_row = connection.execute(
            "SELECT approval_json, responsibility_json "
            "FROM terminal_answer_audits WHERE request_id = ?",
            (ready.request_id,),
        ).fetchone()
    assert evidence_row is not None
    approval_json, responsibility_json = evidence_row
    assert isinstance(approval_json, str)
    assert isinstance(responsibility_json, str)
    assert '"needs_correction_review":true' in approval_json
    assert '"needs_correction_review":true' in responsibility_json

    reopened = _open(
        capable_path,
        policy=_ExplodingPolicy(),
        resolver=_ExplodingResolver(),
    )
    restored = reopened.by_request(ready.request_id)
    assert restored == bundle
    assert restored is not None
    assert restored.answer_record.needs_correction_review is True
    reopened.close()

    with sqlite3.connect(capable_path) as connection:
        connection.execute(
            "UPDATE terminal_answer_audits SET approval_json = ? WHERE request_id = ?",
            ('{"kind":"not_required","policy_version":"approval-v1"}', ready.request_id),
        )
    tampered = _open(
        capable_path,
        policy=_ExplodingPolicy(),
        resolver=_ExplodingResolver(),
    )
    try:
        with pytest.raises(IncompleteCompletionStateError):
            tampered.by_request(ready.request_id)
    finally:
        tampered.close()

    with sqlite3.connect(capable_path) as connection:
        connection.execute(
            "UPDATE terminal_answer_audits SET approval_json = ?, responsibility_json = ? "
            "WHERE request_id = ?",
            (
                approval_json,
                '{"agent_id":"환불-card-1","owner_id":"담당자-user-1"}',
                ready.request_id,
            ),
        )
        connection.execute(
            "UPDATE answer_records SET needs_correction_review = 0 WHERE request_id = ?",
            (ready.request_id,),
        )
    tandem_tampered = _open(
        capable_path,
        policy=_ExplodingPolicy(),
        resolver=_ExplodingResolver(),
    )
    try:
        with pytest.raises(IncompleteCompletionStateError):
            tandem_tampered.by_request(ready.request_id)
    finally:
        tandem_tampered.close()


class _ExplodingPolicy(_Policy):
    def __init__(self) -> None:
        super().__init__(NoApprovalRequired(policy_version="unused"))

    def evaluate(
        self,
        org_id: str,
        route: RouteTarget,
        candidate_mode: AnswerMode,
    ) -> NoApprovalRequired | ApprovalRequired:
        raise AssertionError("policy must not run")


class _ExplodingResolver(_Resolver):
    def resolve(
        self,
        *,
        org_id: str,
        route: RouteTarget,
    ) -> AnswerResponsibilitySnapshot | None:
        raise AssertionError("resolver must not run")


@pytest.mark.parametrize(
    ("session_id", "sources", "snapshot_sha"),
    [
        (None, (), None),
        ("세션-2", ("문서 A", "문서 B"), "sha-2"),
    ],
)
def test_session_sources_snapshot의_선택값을_추정없이_보존한다(
    capable_path: Path,
    session_id: str | None,
    sources: tuple[str, ...],
    snapshot_sha: str | None,
) -> None:
    store = _open(capable_path)
    ready = _ready(store, session_id=session_id)

    store.complete(_handoff(ready, sources=sources, snapshot_sha=snapshot_sha))
    bundle = store.by_request(ready.request_id)

    assert bundle is not None
    assert bundle.answer_record.sources == sources
    assert bundle.answer_record.snapshot_sha == snapshot_sha
    assert (bundle.session_turn is None) == (session_id is None)


def test_public_CAS로_AnsweredRequest를_우회할_수_없다(capable_path: Path) -> None:
    store = _open(capable_path)
    ready = _ready(store)
    answered = ready.transition(
        AnsweredRequest(record_id="위조-record"),
        clock=lambda: NOW,
    )

    with pytest.raises(DirectAnsweredTransitionError):
        store.compare_and_set(ready.request_id, ready.revision, ready, answered)

    assert store.get(ready.request_id) == ready
    assert store.by_request(ready.request_id) is None


def test_다른_handoff_재시도는_restart_뒤에도_concurrency_error다(
    capable_path: Path,
) -> None:
    store = _open(capable_path)
    ready = _ready(store)
    store.complete(_handoff(ready))
    store.close()
    reopened = _open(capable_path)

    with pytest.raises(CompletionConcurrencyError):
        reopened.complete(_handoff(ready, text="서로 다른 답"))

    assert reopened.by_request(ready.request_id) is not None


def test_승인수정된_draft는_full_answer와_exact_approval_evidence로_복원한다(
    capable_path: Path,
) -> None:
    approvals = InMemoryApprovalStore()
    policy = _Policy(ApprovalRequired(approver_id="법무-user", policy_version="approval-v2"))
    store = _open(capable_path, approvals=approvals, policy=policy)
    ready = _ready(store, session_id=None, requires_approval=True)
    assert isinstance(ready.state, ReadyToDispatch)
    item_id = "승인-item-1"
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
        clock=lambda: NOW - timedelta(seconds=40),
    )
    assert store.compare_and_set(ready.request_id, ready.revision, ready, awaiting)
    original = AnswerCandidate(
        text="초안",
        sources=("법무규정.md",),
        mode="draft_only",
        snapshot_sha="legal-sha",
    )
    action = ApproveWithEdit(
        by_approver="법무-user",
        edited_text="법무 검토 완료 답",
    )
    draft = ApprovalDraft(
        draft_id="초안-draft-1",
        request_id=ready.request_id,
        attempt=1,
        route=ready.state.route,
        candidate=original,
        created_at=NOW - timedelta(seconds=30),
    )
    requirement = policy.result
    assert isinstance(requirement, ApprovalRequired)
    open_item = ApprovalItem(
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
        candidate=original.model_copy(update={"text": "법무 검토 완료 답"}),
        approved_by="법무-user",
        approved_at=NOW - timedelta(seconds=10),
        edited=True,
        policy_version="approval-v2",
        assignment_generation=ApprovalAssignmentGeneration.from_item(open_item),
    )
    approvals.create_or_get(open_item)
    approvals.resolve_if_open(
        item_id,
        action,
        lambda item: item.resolve(
            action=action,
            approved_candidate=approved,
            resolved_at=approved.approved_at,
        ),
    )

    completion = store.complete(approved)
    bundle = store.by_request(ready.request_id)

    assert completion.mode == "full"
    assert completion.text == "법무 검토 완료 답"
    assert bundle is not None
    assert bundle.terminal_audit.candidate_mode == "draft_only"
    assert bundle.terminal_audit.approval.kind == "approved"
    assert bundle.terminal_audit.approval.action == "approve_with_edit"
    assert bundle.session_turn is None
    same_instant_different_offset = approved.model_copy(
        update={"approved_at": approved.approved_at.astimezone(timezone.utc)}
    )
    assert same_instant_different_offset.approved_at == approved.approved_at
    assert same_instant_different_offset.approved_at.isoformat() != approved.approved_at.isoformat()
    with pytest.raises(CompletionConcurrencyError):
        store.complete(same_instant_different_offset)

    store.close()
    reopened = _open(capable_path, approvals=approvals, policy=policy)
    try:
        assert reopened.complete(approved) == completion
        restored = reopened.by_request(ready.request_id)
        assert restored is not None
        assert restored.completion == completion
        assert restored.terminal_audit.approval == bundle.terminal_audit.approval
    finally:
        reopened.close()

    with sqlite3.connect(capable_path) as connection:
        row = connection.execute(
            "SELECT handoff_json, handoff_schema_version "
            "FROM question_completion_receipts "
            "WHERE request_id = ?",
            (ready.request_id,),
        ).fetchone()
        assert row is not None
        assert row[1] == 2
        v2_json = row[0]
        connection.execute(
            "UPDATE question_completion_receipts SET handoff_schema_version = 1 "
            "WHERE request_id = ?",
            (ready.request_id,),
        )

    mislabeled_v2 = _open(capable_path, approvals=approvals, policy=policy)
    try:
        with pytest.raises(IncompleteCompletionStateError):
            mislabeled_v2.by_request(ready.request_id)
    finally:
        mislabeled_v2.close()

    with sqlite3.connect(capable_path) as connection:
        legacy_payload = json.loads(v2_json)
        assert legacy_payload.pop("assignment_generation") is not None
        legacy_json = json.dumps(
            legacy_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        legacy_digest = hashlib.sha256(legacy_json.encode("utf-8")).hexdigest()
        connection.execute(
            "UPDATE question_completion_receipts "
            "SET handoff_json = ?, handoff_sha256 = ?, handoff_schema_version = 2 "
            "WHERE request_id = ?",
            (legacy_json, legacy_digest, ready.request_id),
        )

    mislabeled_v1 = _open(capable_path, approvals=approvals, policy=policy)
    try:
        with pytest.raises(IncompleteCompletionStateError):
            mislabeled_v1.by_request(ready.request_id)
    finally:
        mislabeled_v1.close()

    with sqlite3.connect(capable_path) as connection:
        connection.execute(
            "UPDATE question_completion_receipts SET handoff_schema_version = 1 "
            "WHERE request_id = ?",
            (ready.request_id,),
        )

    legacy = _open(capable_path, approvals=approvals, policy=policy)
    try:
        restored = legacy.by_request(ready.request_id)
        assert restored is not None and restored.completion == completion
        with pytest.raises(CompletionConcurrencyError):
            legacy.complete(approved)
        assert legacy.by_request(ready.request_id) == restored
    finally:
        legacy.close()


def test_v1_FinalizationCandidate_receipt는_exact_read와_same_replay를_허용한다(
    capable_path: Path,
) -> None:
    store = _open(capable_path)
    ready = _ready(store, session_id=None)
    handoff = _handoff(ready)
    completion = store.complete(handoff)
    store.close()

    with sqlite3.connect(capable_path) as connection:
        version = connection.execute(
            "SELECT handoff_schema_version FROM question_completion_receipts"
        ).fetchone()
        assert version is not None and version[0] == 2
        connection.execute("UPDATE question_completion_receipts SET handoff_schema_version = 1")

    legacy = _open(capable_path)
    try:
        bundle = legacy.by_request(ready.request_id)
        assert bundle is not None and bundle.completion == completion
        assert legacy.complete(handoff) == completion
        assert legacy.by_request(ready.request_id) == bundle
    finally:
        legacy.close()


def test_receipt없는_legacy_AnswerRecord는_completion으로_승격하지_않는다(
    capable_path: Path,
) -> None:
    with sqlite3.connect(capable_path) as connection:
        connection.execute(
            "INSERT INTO answer_records ("
            "record_id, question, answer_text, answered_by, agent_id, mode, "
            "session_id, answered_at, needs_correction_review, request_id, "
            "sources_json, snapshot_sha"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-record",
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
    store = _open(capable_path)

    assert store.by_record("legacy-record") is None


@pytest.mark.parametrize("request_exists", [False, True])
@pytest.mark.parametrize(
    ("sources_json", "snapshot_sha", "is_v2_trace"),
    [
        (None, None, False),
        ("[]", None, True),
        (None, "snapshot-v2", True),
    ],
)
def test_receipt없는_request_aware_v2_흔적은_숨기지_않고_legacy_NULL만_None이다(
    capable_path: Path,
    request_exists: bool,
    sources_json: str | None,
    snapshot_sha: str | None,
    is_v2_trace: bool,
) -> None:
    store = _open(capable_path)
    request_id = "request-aware-residual"
    if request_exists:
        _ready(store, request_id=request_id, session_id=None)
    with sqlite3.connect(capable_path) as connection:
        connection.execute(
            "INSERT INTO answer_records ("
            "record_id, question, answer_text, answered_by, agent_id, mode, session_id, "
            "answered_at, needs_correction_review, request_id, sources_json, snapshot_sha"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "receiptless-record",
                "migration 전 질문",
                "migration 전 답",
                "legacy-owner",
                "legacy-card",
                "full",
                None,
                NOW.isoformat(),
                0,
                request_id,
                sources_json,
                snapshot_sha,
            ),
        )

    if is_v2_trace:
        with pytest.raises(IncompleteCompletionStateError):
            store.by_request(request_id)
        with pytest.raises(IncompleteCompletionStateError):
            store.by_record("receiptless-record")
    else:
        assert store.by_request(request_id) is None
        assert store.by_record("receiptless-record") is None


def test_nonterminal_request에_native_artifact가_있으면_fail_closed한다(
    capable_path: Path,
) -> None:
    store = _open(capable_path)
    ready = _ready(store, session_id=None)
    store.close()
    # foreign key가 요구하는 legacy record를 둔 뒤 raw partial outbox를 만든다.
    with sqlite3.connect(capable_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO answer_records ("
            "record_id, question, answer_text, answered_by, agent_id, mode, session_id, "
            "answered_at, needs_correction_review, request_id, sources_json, snapshot_sha"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "partial-record",
                ready.question,
                "부분 답",
                "owner",
                "환불-card-1",
                "full",
                None,
                NOW.isoformat(),
                0,
                ready.request_id,
                "[]",
                None,
            ),
        )
        connection.execute(
            "INSERT INTO question_delivery_outbox "
            "(request_id, record_id, kind, created_at) VALUES (?, ?, ?, ?)",
            (ready.request_id, "partial-record", "answer_ready", NOW.isoformat()),
        )
    reopened = _open(capable_path)

    with pytest.raises(IncompleteCompletionStateError):
        reopened.by_request(ready.request_id)
    with pytest.raises(IncompleteCompletionStateError):
        reopened.complete(_handoff(ready))


def test_nonterminal은_ISO_TEXT가_아니라_실제_aware_datetime순으로_정렬한다(
    capable_path: Path,
) -> None:
    store = _open(capable_path)
    earlier = datetime(2026, 1, 1, 1, 0, tzinfo=timezone(timedelta(hours=2)))
    later = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
    assert earlier < later
    first = QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        question="실제 시각이 더 이른 질문",
        request_id_factory=lambda: "req-z",
        clock=lambda: earlier,
        due_at=earlier + timedelta(hours=1),
    )
    second = QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        question="실제 시각이 더 늦은 질문",
        request_id_factory=lambda: "req-a",
        clock=lambda: later,
        due_at=later + timedelta(hours=1),
    )
    store.create(second)
    store.create(first)

    assert [request.request_id for request in store.nonterminal()] == ["req-z", "req-a"]


def test_public_read와_complete_반환값은_SQLite_backing_state의_alias가_아니다(
    capable_path: Path,
) -> None:
    store = _open(capable_path)
    ready = _ready(store)
    completion = store.complete(_handoff(ready))
    bundle = store.by_request(ready.request_id)
    exposed_request = store.get(ready.request_id)
    assert bundle is not None and exposed_request is not None

    object.__setattr__(completion, "text", "호출자 변조")
    object.__setattr__(bundle.answer_record, "answer_text", "호출자 변조")
    object.__setattr__(exposed_request, "question", "호출자 변조")

    restored = store.by_request(ready.request_id)
    assert restored is not None
    assert restored.completion.text == "영업일 기준 3일 안에 처리됩니다. ✅"
    assert restored.answer_record.answer_text == restored.completion.text
    assert restored.request.question == "환불은 언제 처리되나요? 💳"
