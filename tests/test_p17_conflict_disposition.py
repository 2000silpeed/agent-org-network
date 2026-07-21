from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.answer_finalization import CompletionBundle
from agent_org_network.conflict import Candidate, ConflictCase
from agent_org_network.demo_question_surfaces import DemoRouteAuthority
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.p17_conflict_disposition import (
    ConcurOnConflict,
    ConcurrencePending,
    ConflictResolved,
    InMemoryConflictDispositionStore,
    OwnerPrincipal,
    P17DirectConflictDispositionApplication,
    SupportingKnowledgeEvidence,
)
from agent_org_network.p17_manager_disposition import ExecutionStarted
from agent_org_network.question_request import (
    AwaitingConflict,
    HandlingAssignment,
    InMemoryQuestionRequestStore,
    QuestionRequest,
    ReadyToDispatch,
)
from agent_org_network.registry import Registry
from agent_org_network.user import User


NOW = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)


def _ids(*values: str) -> Callable[[], object]:
    iterator: Iterator[str] = iter(values)
    return lambda: next(iterator)


def _card(
    agent_id: str,
    owner: str,
    *,
    approval_when: tuple[str, ...] = (),
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="support",
        summary=agent_id,
        domains=["refund"],
        last_reviewed_at=date(2026, 7, 1),
        approval_when=list(approval_when),
    )


class _DeadlinePolicy:
    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        del org_id, state_kind
        return started_at + timedelta(hours=1)


class _Starter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def ensure_started(self, request_id: str) -> ExecutionStarted:
        self.calls.append(request_id)
        return ExecutionStarted()


class _CompletionReader:
    def by_request(self, request_id: str) -> CompletionBundle | None:
        del request_id
        return None

    def by_record(self, record_id: str) -> CompletionBundle | None:
        del record_id
        return None


def _fixture(
    *,
    target_approval_when: tuple[str, ...] = (),
    central_authorizer: object | None = None,
    current_owner_a: str = "owner-a",
) -> tuple[
    P17DirectConflictDispositionApplication,
    InMemoryQuestionRequestStore,
    InMemoryConflictDispositionStore,
    _Starter,
]:
    registry = Registry()
    registry.register_user(User(id="owner-a"))
    registry.register_user(User(id="owner-b"))
    if current_owner_a not in {"owner-a", "owner-b"}:
        registry.register_user(User(id=current_owner_a))
    registry.register(_card("refund-card", current_owner_a, approval_when=target_approval_when))
    registry.register(_card("finance-card", "owner-b"))

    requests = InMemoryQuestionRequestStore()
    received = QuestionRequest.receive(
        org_id="demo-org",
        requester_id="requester",
        question="환불 기준은?",
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        due_at=NOW + timedelta(hours=1),
    )
    requests.create(received)
    awaiting = received.record_initial_routing(
        intent="refund",
        disposition="contested",
        target=AwaitingConflict(
            case_id="case-1",
            handling=HandlingAssignment(
                kind="conflict_case",
                ref="case-1",
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW,
    )
    assert requests.compare_and_set("request-1", 0, received, awaiting)

    conflicts = InMemoryConflictDispositionStore(
        id_factory=_ids("generation-1", "control-1", "forward-1")
    )
    conflicts.create_or_get_for_request(
        ConflictCase.for_request(
            request_id="request-1",
            intent="refund",
            question="환불 기준은?",
            candidates=(
                Candidate(agent_id="refund-card", owner="owner-a"),
                Candidate(agent_id="finance-card", owner="owner-b"),
            ),
            opened_at=NOW,
            case_id="case-1",
        )
    )
    starter = _Starter()
    app = P17DirectConflictDispositionApplication(
        requests=requests,
        conflicts=conflicts,
        registry=registry,
        route_authority=DemoRouteAuthority(registry),
        completion_reader=_CompletionReader(),
        deadline_policy=_DeadlinePolicy(),
        execution_starter=starter,
        clock=lambda: NOW,
        central_authorizer=central_authorizer,  # type: ignore[arg-type]
    )
    return app, requests, conflicts, starter


class _AllowingConflictAuthorizer:
    def __init__(self, *, verify_result: bool = True, unavailable: bool = False) -> None:
        self.calls: list[tuple[object, object, object]] = []
        self.verify_calls = 0
        self.verify_result = verify_result
        self.unavailable = unavailable

    def authorize(self, principal: object, action: object, resource: object) -> object:
        self.calls.append((principal, action, resource))
        if self.unavailable:
            from agent_org_network.central_authority import AuthorizationDenied

            return AuthorizationDenied(kind="policy_unavailable")
        assert type(principal) is AuthenticatedPrincipal
        assert type(resource) is ResourceRef
        return AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=action,  # type: ignore[arg-type]
            resource=resource,
            roles=("owner",),
            policy_version="policy-v1",
            policy_digest="b" * 64,
        )

    def verify(self, grant: object, principal: object, action: object, resource: object) -> bool:
        del grant, principal, action, resource
        self.verify_calls += 1
        return self.verify_result


def test_central_conflict_concurrence_requires_authenticated_candidate_and_sealed_grant() -> None:
    authorizer = _AllowingConflictAuthorizer()
    app, _requests, _conflicts, _starter = _fixture(central_authorizer=authorizer)
    principal = AuthenticatedPrincipal(
        org_id="demo-org",
        subject_id="owner-a",
        identity_provider="oidc",
        identity_session_id="session-a",
    )

    result = app.concur(
        ConcurOnConflict(
            principal=principal,
            case_id="case-1",
            expected_round=1,
            on_agent="refund-card",
        )
    )

    assert isinstance(result, ConcurrencePending)
    assert authorizer.verify_calls == 1
    assert authorizer.calls[0][1:] == (
        "conflict.concur",
        ResourceRef(
            org_id="demo-org",
            kind="conflict_case",
            resource_id="case-1",
            owner_subject_id="owner-a",
        ),
    )


@pytest.mark.parametrize(
    ("authorizer", "expected_error"),
    [
        (_AllowingConflictAuthorizer(verify_result=False), "denied"),
        (_AllowingConflictAuthorizer(unavailable=True), "unavailable"),
    ],
)
def test_central_conflict_rejects_forged_or_unavailable_without_vote_write(
    authorizer: _AllowingConflictAuthorizer,
    expected_error: str,
) -> None:
    from agent_org_network.p17_conflict_disposition import (
        ConflictAuthorizationUnavailable,
        ConflictDispositionNotFoundOrDenied,
    )

    app, _requests, conflicts, _starter = _fixture(central_authorizer=authorizer)
    principal = AuthenticatedPrincipal(
        org_id="demo-org",
        subject_id="owner-a",
        identity_provider="oidc",
        identity_session_id="session-a",
    )
    error_type = (
        ConflictAuthorizationUnavailable
        if expected_error == "unavailable"
        else ConflictDispositionNotFoundOrDenied
    )

    with pytest.raises(error_type) as caught:
        app.concur(
            ConcurOnConflict(
                principal=principal,
                case_id="case-1",
                expected_round=1,
                on_agent="refund-card",
            )
        )

    assert caught.value.args == ()
    assert caught.value.__cause__ is None
    assert conflicts.progress_history_for_case("case-1") == ()


def test_central_conflict_rejects_stale_stored_candidate_owner_before_authorizer_or_write() -> None:
    from agent_org_network.p17_conflict_disposition import ConflictDispositionNotFoundOrDenied

    authorizer = _AllowingConflictAuthorizer()
    app, _requests, conflicts, _starter = _fixture(
        central_authorizer=authorizer,
        current_owner_a="owner-c",
    )
    principal = AuthenticatedPrincipal(
        org_id="demo-org",
        subject_id="owner-a",
        identity_provider="oidc",
        identity_session_id="session-a",
    )

    with pytest.raises(ConflictDispositionNotFoundOrDenied):
        app.concur(
            ConcurOnConflict(
                principal=principal,
                case_id="case-1",
                expected_round=1,
                on_agent="refund-card",
            )
        )

    assert authorizer.calls == []
    assert conflicts.progress_history_for_case("case-1") == ()


def test_central_conflict_list_and_document_use_separate_actions() -> None:
    authorizer = _AllowingConflictAuthorizer()
    app, _requests, _conflicts, _starter = _fixture(central_authorizer=authorizer)
    principal = AuthenticatedPrincipal(
        org_id="demo-org",
        subject_id="owner-a",
        identity_provider="oidc",
        identity_session_id="session-a",
    )

    pending = app.pending_for(principal)
    document = app.document("case-1", principal)

    assert [case.case_id for case in pending] == ["case-1"]
    assert document.case_id == "case-1"
    assert [call[1] for call in authorizer.calls] == [
        "conflict.list",
        "conflict.document.read",
    ]
    assert authorizer.verify_calls == 2


def test_authenticated_conflict_principal_without_central_authorizer_is_fail_closed() -> None:
    from agent_org_network.p17_conflict_disposition import ConflictDispositionNotFoundOrDenied

    app, _requests, conflicts, _starter = _fixture()
    principal = AuthenticatedPrincipal(
        org_id="demo-org",
        subject_id="owner-a",
        identity_provider="oidc",
        identity_session_id="session-a",
    )

    with pytest.raises(ConflictDispositionNotFoundOrDenied):
        app.concur(
            ConcurOnConflict(
                principal=principal,
                case_id="case-1",
                expected_round=1,
                on_agent="refund-card",
            )
        )

    assert conflicts.progress_history_for_case("case-1") == ()


def _command(
    owner: str,
    *,
    stance: Literal["withdraw", "keep_as_complement"] = "withdraw",
    rationale: str = "",
) -> ConcurOnConflict:
    return ConcurOnConflict(
        principal=OwnerPrincipal(org_id="demo-org", subject_id=owner),
        case_id="case-1",
        expected_round=1,
        on_agent="refund-card",
        stance=stance,
        rationale=rationale,
    )


def test_direct_application은_pending뒤_evidence_CAS_case_wake순으로_같은Request를_재개한다() -> (
    None
):
    app, requests, conflicts, starter = _fixture(target_approval_when=("refund",))

    pending = app.concur(_command("owner-a", rationale="A"))
    resolved = app.concur(
        _command(
            "owner-b",
            stance="keep_as_complement",
            rationale="B",
        )
    )

    assert pending == ConcurrencePending(
        request_id="request-1",
        case_id="case-1",
        current_round=1,
        pending_owners=("owner-b",),
    )
    assert isinstance(resolved, ConflictResolved)
    assert resolved.route.agent_id == "refund-card"
    assert resolved.route.requires_approval is True
    stored_request = requests.get("request-1")
    assert stored_request is not None
    assert isinstance(stored_request.state, ReadyToDispatch)
    assert stored_request.state.attempt == 1
    assert stored_request.state.trigger_key == "request-dispatch:request-1:1"
    stored_case = conflicts.get_request_case("case-1")
    assert stored_case is not None
    assert stored_case.status == "resolved"
    assert stored_case.resolution is not None
    assert stored_case.resolution.rationale == "owner-a→refund-card; owner-b→refund-card"
    evidence = conflicts.resolution_evidence_for_request("request-1")
    assert evidence is not None
    assert evidence.supporting == (
        SupportingKnowledgeEvidence(
            agent_id="finance-card",
            affirmed_by_owner="owner-b",
        ),
    )
    assert starter.calls == ["request-1"]


def test_direct_application은_관련없는_approval_when을_승인필요로_보지않는다() -> None:
    app, _requests, _conflicts, _starter = _fixture(target_approval_when=("contract",))

    app.concur(_command("owner-a"))
    resolved = app.concur(_command("owner-b"))

    assert isinstance(resolved, ConflictResolved)
    assert resolved.route.requires_approval is False


def test_direct_application은_case_intent가_approval_when에_있으면_승인을_요구한다() -> None:
    app, _requests, _conflicts, _starter = _fixture(target_approval_when=("refund",))

    app.concur(_command("owner-a"))
    resolved = app.concur(_command("owner-b"))

    assert isinstance(resolved, ConflictResolved)
    assert resolved.route.requires_approval is True


def test_direct_application은_같은_terminal_action_retry에_grant와_evidence를_늘리지않는다() -> (
    None
):
    app, _requests, conflicts, starter = _fixture()
    app.concur(_command("owner-a"))
    first = app.concur(_command("owner-b"))
    before = conflicts.progress_history_for_case("case-1")

    retry = app.concur(_command("owner-b"))

    assert retry == first
    assert conflicts.progress_history_for_case("case-1") == before
    assert starter.calls == ["request-1", "request-1"]


def test_p17_conflict_module은_Router_Precedent_ComplementEdge를_import하지않는다() -> None:
    path = Path(__file__).parents[1] / "src" / "agent_org_network" / "p17_conflict_disposition.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {"router", "complement"}
    imported = {
        alias.name.split(".")[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        (node.module or "").split(".")[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert imported.isdisjoint(forbidden)
